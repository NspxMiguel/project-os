"""Meta's official WhatsApp Cloud API.

Needs a Meta Business account, a WhatsApp phone number registered against it,
and a permanent access token (see ``docs/WHATSAPP.md``). In exchange: no
unofficial client pretending to be a phone, no ban risk, a real support
channel -- and, past a small monthly allowance of conversations, a bill.

The webhook is the dangerous part. Anyone who finds the URL can POST a fake
message unless every request's ``X-Hub-Signature-256`` is checked against an
HMAC computed with the app secret, in constant time. Skipping that turns
"project-os can receive WhatsApp messages" into "anyone on the internet can
inject WhatsApp messages into project-os".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List, Optional

from .base import Provider, ProviderError

#: Bumped occasionally by Meta; not worth making configurable for one call site.
GRAPH_API_VERSION = "v20.0"
GRAPH_BASE_URL = "https://graph.facebook.com"


class CloudApiProvider(Provider):
    name = "cloud_api"

    def __init__(self, config: Optional[Dict[str, Any]], logger: Optional[logging.Logger] = None) -> None:
        super().__init__(config, logger)
        self.phone_number_id = str(self.config.get("phone_number_id") or "")
        self.access_token = str(self.config.get("access_token") or "")
        self.verify_token = str(self.config.get("verify_token") or "")
        self.app_secret = str(self.config.get("app_secret") or "")

    def status(self) -> Dict[str, Any]:
        configured = bool(self.phone_number_id and self.access_token)
        return {
            "configured": configured,
            # Um token escrito no config não é um token aceito pela Meta.
            "connected": None if configured else False,
            "mode": self.name,
            "phone_number_id": self.phone_number_id or None,
            "reason": None if configured else "missing phone_number_id or access_token",
        }

    async def probe(self) -> Dict[str, Any]:
        """Pergunta pelo próprio número: é a checagem que valida o token."""
        if not (self.phone_number_id and self.access_token):
            return {"connected": False, "reason": "falta phone_number_id ou access_token"}
        try:
            import httpx
        except ImportError:
            return {"connected": None, "reason": "sem httpx não dá para checar a Cloud API"}
        url = "%s/%s/%s" % (GRAPH_BASE_URL, GRAPH_API_VERSION, self.phone_number_id)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    url, headers={"Authorization": "Bearer %s" % self.access_token}
                )
        except Exception as exc:  # noqa: BLE001
            return {"connected": False,
                    "reason": "não consegui falar com a Meta (%s)" % type(exc).__name__}
        if response.status_code in (401, 403):
            return {"connected": False, "reason": "a Meta recusou o token"}
        if response.status_code >= 400:
            return {"connected": False,
                    "reason": "a Meta respondeu HTTP %s" % response.status_code}
        return {"connected": True, "reason": None}

    async def send_text(self, to: str, text: str) -> Dict[str, Any]:
        if not (self.phone_number_id and self.access_token):
            raise ProviderError(
                "The Cloud API provider is not configured: set phone_number_id and access_token."
            )
        try:
            import httpx  # optional dependency; only needed for outbound calls
        except ImportError as exc:
            raise ProviderError(
                "The Cloud API provider needs httpx. Install it with: pip install httpx"
            ) from exc
        url = "%s/%s/%s/messages" % (GRAPH_BASE_URL, GRAPH_API_VERSION, self.phone_number_id)
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        headers = {"Authorization": "Bearer %s" % self.access_token}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise ProviderError(
                "The Cloud API refused the message (HTTP %s): %s"
                % (response.status_code, response.text[:300])
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        return {"ok": True, "provider": self.name, "delivered": True, "response": body}

    def verify_challenge(self, query: Dict[str, str]) -> Optional[str]:
        if query.get("hub.mode") != "subscribe":
            return None
        token = query.get("hub.verify_token") or ""
        if not self.verify_token or not hmac.compare_digest(token, self.verify_token):
            return None
        return query.get("hub.challenge")

    def verify_signature(self, headers: Dict[str, str], raw_body: bytes) -> bool:
        if not self.app_secret:
            self.log.warning(
                "cloud_api.app_secret is not set; refusing an inbound webhook I cannot authenticate"
            )
            return False
        header = headers.get("x-hub-signature-256", "")
        if not header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header[len("sha256="):], expected)

    def parse_inbound(self, raw_body: bytes) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        out = []  # type: List[Dict[str, Any]]
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                for message in value.get("messages", []) or []:
                    if message.get("type") != "text":
                        continue  # media/reactions/etc aren't commands; skip rather than guess
                    body = (message.get("text") or {}).get("body", "")
                    out.append(
                        {"from": message.get("from", ""), "text": body, "id": message.get("id", "")}
                    )
        return out


__all__ = ["CloudApiProvider", "GRAPH_API_VERSION", "GRAPH_BASE_URL"]
