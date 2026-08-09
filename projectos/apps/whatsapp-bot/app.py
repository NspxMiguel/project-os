"""setup(ctx) -> AppInstance: HTTP routes, inbound dispatch, provider lifecycle.

See the package docstring in ``__init__.py`` for the why. This module is the
how: it owns the FastAPI router, the message table, the provider instance,
and the loop that turns an inbound WhatsApp message into a dispatched command
and (maybe) a reply.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from projectos import auth
from projectos.config import is_redacted
from projectos.core.plugins import AppContext, AppInstance
from projectos.core import sysinfo
from projectos.db import rows_to_dicts, utcnow_iso
from projectos.errors import ApiError

from .commands import CommandRegistry, UnknownCommand
from .providers import Provider, ProviderError, build_provider
from .ratelimit import RateLimiter

APP_ID = "whatsapp-bot"
SCHEMA_NAME = "whatsapp_bot"
TABLE_MESSAGES = "app_whatsapp_bot_messages"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS app_whatsapp_bot_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        direction TEXT NOT NULL,
        contact TEXT NOT NULL,
        body TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT '',
        command TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_bot_messages_created_at "
    "ON app_whatsapp_bot_messages (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_whatsapp_bot_messages_contact "
    "ON app_whatsapp_bot_messages (contact)",
]

# Cross-app command registration: any other app can add a command without
# importing this module -- see commands.py's docstring for the payload shape.
REGISTER_TOPIC = "whatsapp.command.register"
UNREGISTER_TOPIC = "whatsapp.command.unregister"

DEFAULT_RATE_LIMIT = 10
DEFAULT_WINDOW_SECONDS = 60.0
MAX_HISTORY_ROWS = 200


def _digits(value: str) -> str:
    """Normalize a phone number for comparison: keep only the digits.

    "+55 11 91234-5678", "5511912345678" and "55 (11) 91234 5678" all mean the
    same contact to a human filling in the allowlist; comparing raw strings
    would silently reject two of those three spellings.
    """
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _split_command(text: str, prefix: str) -> Optional[Tuple[str, List[str]]]:
    """("!status extra words", "!") -> ("status", ["extra", "words"]). None if not a command."""
    stripped = (text or "").strip()
    if not prefix or not stripped.startswith(prefix):
        return None
    body = stripped[len(prefix):].strip()
    if not body:
        return None
    parts = body.split()
    return parts[0].lower(), parts[1:]


def _is_allowed(contact: str, allowlist: Optional[List[str]]) -> bool:
    """Default-deny: an empty allowlist means nobody is allowed yet, on purpose."""
    entries = {_digits(entry) for entry in (allowlist or []) if str(entry).strip()}
    return bool(entries) and _digits(contact) in entries


class SendBody(object):
    """Body shape for POST /send, read manually (see build_router) to avoid
    pinning a pydantic model version-specific style for two fields."""


def register_builtin_commands(registry: CommandRegistry) -> None:
    def cmd_help(context: Any) -> str:
        names = registry.names()
        return "Comandos: " + ", ".join(names) if names else "Nenhum comando registrado."

    def cmd_apps(context: Any) -> str:
        request = context.request
        if request is None:
            return "Lista de apps indisponivel fora de uma requisicao HTTP."
        from projectos.main import get_plugins  # local import: avoids a hard import cycle at module load
        from projectos.core.plugins import STATE_RUNNING

        plugins = get_plugins(request)
        running = [r["id"] for r in plugins.list_apps() if r.get("state") == STATE_RUNNING]
        return "Apps rodando: " + ", ".join(sorted(running)) if running else "Nenhum app rodando."

    def cmd_status(context: Any) -> str:
        stats = sysinfo.stats()
        cpu = stats.get("cpu_percent")
        mem = (stats.get("memory") or {}).get("percent")
        temp = stats.get("temp_c")
        disks = stats.get("disks") or []
        disk_pct = disks[0].get("percent") if disks else None
        parts = []
        if cpu is not None:
            parts.append("CPU %.0f%%" % cpu)
        if mem is not None:
            parts.append("RAM %.0f%%" % mem)
        if temp is not None:
            parts.append("%.0fC" % temp)
        if disk_pct is not None:
            parts.append("disco %.0f%%" % disk_pct)
        return "Status: " + ", ".join(parts) if parts else "Status indisponivel."

    registry.register("help", cmd_help, "help -- lista os comandos disponiveis")
    registry.register("apps", cmd_apps, "apps -- lista os apps instalados que estao rodando")
    registry.register("status", cmd_status, "status -- CPU, memoria, temperatura e disco do sistema")


def build_router(instance: "WhatsAppBotApp") -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    async def get_status(_: Any = Depends(auth.require_auth)) -> Dict[str, Any]:
        return instance.status()

    @router.get("/config")
    async def get_config(_: Any = Depends(auth.require_auth)) -> Dict[str, Any]:
        return {"config": instance.ctx.config.as_dict(redact=True)}

    @router.put("/config")
    async def put_config(patch: Dict[str, Any], _: Any = Depends(auth.require_auth)) -> Dict[str, Any]:
        for path, value in patch.items():
            if is_redacted(value):
                continue  # the panel echoed back a masked secret it never saw; don't clobber the real one
            instance.ctx.config.set(path, value)
        instance.ctx.config.save()
        await instance._rebuild_provider()
        return {"config": instance.ctx.config.as_dict(redact=True)}

    @router.post("/send")
    async def send(body: Dict[str, Any], _: Any = Depends(auth.require_auth)) -> Dict[str, Any]:
        to = str(body.get("to") or "").strip()
        text = str(body.get("text") or "").strip()
        if not to or not text:
            raise ApiError(400, "bad_request", "Informe 'to' e 'text'.")
        try:
            return await instance._send(to, text)
        except ProviderError as exc:
            raise ApiError(502, "provider_error", str(exc)) from exc

    @router.get("/messages")
    async def get_messages(limit: int = 50, _: Any = Depends(auth.require_auth)) -> Dict[str, Any]:
        capped = max(1, min(int(limit), MAX_HISTORY_ROWS))
        return {"messages": instance._history(capped)}

    # The two webhook routes are the app's only unauthenticated surface, by
    # design: Meta (or a bridge) cannot present a ProjectOS session cookie, so
    # they are protected by the provider's own signature/token check instead.
    @router.get("/webhook")
    async def webhook_verify(request: Request) -> PlainTextResponse:
        provider = instance.provider
        challenge = provider.verify_challenge(dict(request.query_params)) if provider else None
        if challenge is None:
            raise ApiError(403, "forbidden", "Verificacao do webhook falhou.")
        return PlainTextResponse(challenge)

    @router.post("/webhook")
    async def webhook_receive(request: Request) -> Dict[str, Any]:
        provider = instance.provider
        if provider is None:
            raise ApiError(503, "unavailable", "Nenhum provedor configurado.")
        raw_body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not provider.verify_signature(headers, raw_body):
            raise ApiError(401, "unauthenticated", "Assinatura do webhook invalida.")
        messages = provider.parse_inbound(raw_body)
        for message in messages:
            await instance._handle_inbound(
                message.get("from", ""), message.get("text", ""), raw=message, request=request
            )
        return {"received": len(messages)}

    return router


class WhatsAppBotApp(AppInstance):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.log = getattr(ctx, "logger", None) or logging.getLogger("projectos.apps.whatsapp-bot")
        self.registry = CommandRegistry()
        register_builtin_commands(self.registry)
        self.provider = None  # type: Optional[Provider]
        self._rate_limiter = RateLimiter(window_seconds=DEFAULT_WINDOW_SECONDS)
        self._register_sub = None  # type: Optional[Any]
        self._register_task = None  # type: Optional[asyncio.Task]
        self.router = build_router(self)

    async def start(self) -> None:
        self.ctx.db.register_schema(SCHEMA_NAME, SCHEMA_STATEMENTS)
        await self._rebuild_provider()
        self._register_sub = self.ctx.bus.subscribe([REGISTER_TOPIC, UNREGISTER_TOPIC])
        self._register_task = asyncio.create_task(self._consume_registrations())

    async def stop(self) -> None:
        if self._register_task is not None:
            self._register_task.cancel()
            try:
                await self._register_task
            except asyncio.CancelledError:
                pass
            self._register_task = None
        if self._register_sub is not None:
            close = getattr(self._register_sub, "close", None)
            if callable(close):
                close()
            self._register_sub = None
        if self.provider is not None:
            try:
                await self.provider.disconnect()
            except Exception:  # a provider misbehaving on shutdown must not block the app from stopping
                self.log.exception("error disconnecting the whatsapp provider during stop")

    def status(self) -> Dict[str, Any]:
        allowlist = self.ctx.config.get("allowlist", []) or []
        base = {
            "provider": self.provider.name if self.provider else "null",
            "commands": self.registry.names(),
            "allowlist_size": len(allowlist),
        }
        if self.provider is not None:
            base.update(self.provider.status())
        # See the app-status convention at the top of web/views/dashboard.js: with
        # no summary/fields the card dumps every key, including the provider's own
        # "reason" sentence, which is a diagnostic and not a home-screen line.
        connected = bool(base.get("connected"))
        if base["provider"] == "null":
            base["summary"] = "No provider set up yet -- messages are only logged."
        elif connected:
            base["summary"] = "Connected through %s." % base["provider"]
        else:
            base["summary"] = str(base.get("reason") or "Not connected.")
        base["fields"] = [
            {"label": "Provider", "value": base["provider"], "kind": "text"},
            {"label": "Connected", "value": connected, "kind": "boolean"},
            {"label": "Allowed contacts", "value": base["allowlist_size"], "kind": "number"},
            {"label": "Commands", "value": base["commands"], "kind": "list"},
        ]
        return base

    async def _rebuild_provider(self) -> None:
        if self.provider is not None:
            try:
                await self.provider.disconnect()
            except Exception:
                self.log.exception("error disconnecting the previous whatsapp provider")
        name = self.ctx.config.get("provider", "null")
        self.provider = build_provider(name, self._provider_config(name), self.log)
        try:
            await self.provider.connect()
        except Exception:
            self.log.exception("error connecting the whatsapp provider %r", name)

    def _provider_config(self, name: Optional[str] = None) -> Dict[str, Any]:
        key = name or self.ctx.config.get("provider", "null")
        return self.ctx.config.get(key, {}) or {}

    async def _consume_registrations(self) -> None:
        assert self._register_sub is not None
        try:
            async for event in self._register_sub:
                try:
                    if event.topic == REGISTER_TOPIC:
                        data = event.data or {}
                        name = data.get("name")
                        handler = data.get("handler")
                        if name and callable(handler):
                            self.registry.register(name, handler, data.get("help", ""))
                    elif event.topic == UNREGISTER_TOPIC:
                        data = event.data or {}
                        name = data.get("name")
                        if name:
                            self.registry.unregister(name)
                except Exception:
                    self.log.warning("ignoring a malformed whatsapp command registration", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def _handle_inbound(
        self, from_: str, text: str, raw: Optional[Dict[str, Any]] = None, request: Optional[Request] = None
    ) -> None:
        contact = _digits(from_)
        allowlist = self.ctx.config.get("allowlist", []) or []
        allowed = _is_allowed(contact, allowlist)
        self._store_message(
            "in", contact, text, "received" if allowed else "blocked",
            command="", provider=self.provider.name if self.provider else "null",
        )
        self.ctx.emit("message.in", {"contact": contact, "text": text, "allowed": allowed})
        if not allowed:
            self.log.info("ignoring an inbound message from a contact not on the allowlist: %s", contact)
            return

        limit = int(self.ctx.config.get("rate_limit.max_per_minute", DEFAULT_RATE_LIMIT))
        if not self._rate_limiter.allow(contact, limit):
            self.ctx.emit("rate_limited", {"contact": contact})
            self.log.info("rate limit hit for contact %s", contact)
            return

        prefix = str(self.ctx.config.get("commands.prefix", "!") or "!")
        parsed = _split_command(text, prefix)
        if parsed is None:
            return  # not a command; the bot only ever speaks when spoken to with the prefix
        name, args = parsed
        try:
            reply = await self.registry.dispatch(name, args, text, contact, request=request)
        except UnknownCommand:
            reply = "Comando desconhecido: %s" % name
        except Exception as exc:  # a broken command handler must not take the webhook down with it
            self.log.exception("command %r failed", name)
            reply = "O comando falhou: %s" % exc

        if reply:
            try:
                await self._send(contact, reply, command=name)
            except ProviderError:
                self.log.exception("failed to send the reply to %s", contact)

    async def _send(self, to: str, text: str, command: str = "") -> Dict[str, Any]:
        if self.provider is None:
            raise ProviderError("Nenhum provedor configurado.")
        result = await self.provider.send_text(to, text)
        self._store_message("out", _digits(to), text, "sent", command=command, provider=self.provider.name)
        self.ctx.emit("message.out", {"contact": _digits(to), "text": text, "command": command})
        return result

    def _store_message(
        self, direction: str, contact: str, body: str, status: str, command: str = "", provider: str = ""
    ) -> None:
        self.ctx.db.execute(
            "INSERT INTO %s (direction, contact, body, provider, command, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)" % TABLE_MESSAGES,
            (direction, contact, body, provider, command, status, utcnow_iso()),
        )

    def _history(self, limit: int) -> List[Dict[str, Any]]:
        rows = self.ctx.db.query(
            "SELECT * FROM %s ORDER BY id DESC LIMIT ?" % TABLE_MESSAGES, (limit,)
        )
        return rows_to_dicts(rows)


def setup(ctx: AppContext) -> WhatsAppBotApp:
    return WhatsAppBotApp(ctx)


__all__ = ["WhatsAppBotApp", "setup", "APP_ID"]
