"""The app (plugin) system.

Apps are directories holding a ``manifest.json`` and Python code::

    ~/.project_os/apps/<id>/     user-installed, wins on id collision
    project_os/apps/<id>/        bundled with the release

The manifest names an entrypoint (``"app:setup"``); the manager imports it,
builds an :class:`AppContext`, awaits ``setup(ctx) -> AppInstance`` and starts
it. Everything an app can do wrong -- bad manifest, import error, exception in
``setup``, exception in ``start`` -- is caught, recorded on the app and shown in
the UI. A broken app never takes the box down.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
import importlib.util
import inspect
import json
import logging
import re
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter

from project_os import __version__, paths
from project_os.config import AppConfig, Config
from project_os.core import containers
from project_os.errors import ApiError

log = logging.getLogger(__name__)

APP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
REQUIRED_MANIFEST_KEYS = ("id", "name", "version", "entrypoint")
MAX_TRACEBACK_CHARS = 4000

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_ERROR = "error"
STATE_DISABLED = "disabled"

_IP_CACHE_TTL = 30.0
_ip_cache = {"value": "", "at": 0.0}  # type: Dict[str, Any]


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def primary_ip(ttl: float = _IP_CACHE_TTL) -> str:
    """The LAN address other machines can reach us on.

    Opens a UDP socket "towards" an unroutable address: the kernel picks the
    outbound interface without a single packet leaving the box. Falls back to
    loopback when there is no network at all.
    """
    now = time.monotonic()
    if _ip_cache["value"] and (now - float(_ip_cache["at"])) < ttl:
        return str(_ip_cache["value"])
    address = "127.0.0.1"
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("10.255.255.255", 1))
        found = sock.getsockname()[0]
        if found:
            address = found
    except OSError:
        address = "127.0.0.1"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
    _ip_cache["value"] = address
    _ip_cache["at"] = now
    return address


def _version_tuple(value: str) -> Tuple[int, ...]:
    parts = []  # type: List[int]
    for chunk in re.split(r"[.\-+]", str(value or "")):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) if parts else (0,)


async def _maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _short_traceback(exc: BaseException) -> str:
    text = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    if len(text) > MAX_TRACEBACK_CHARS:
        text = text[: MAX_TRACEBACK_CHARS - 20] + "\n... (truncated)"
    return text


class ManifestError(ValueError):
    """A manifest.json that cannot be trusted."""


def validate_manifest(data: Any, expected_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate and normalise a manifest, filling in the optional keys."""
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    for key in REQUIRED_MANIFEST_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ManifestError("missing or empty required key: %s" % key)
    app_id = data["id"].strip()
    if not APP_ID_RE.match(app_id):
        raise ManifestError(
            "invalid app id %r (must match %s)" % (app_id, APP_ID_RE.pattern)
        )
    if expected_id is not None and app_id != expected_id:
        raise ManifestError(
            "app id %r does not match its directory name %r" % (app_id, expected_id)
        )
    entrypoint = data["entrypoint"].strip()
    if ":" not in entrypoint:
        raise ManifestError(
            "entrypoint must be \"module:function\", got %r" % entrypoint
        )
    module_part, _, func_part = entrypoint.partition(":")
    if not module_part or not func_part:
        raise ManifestError("entrypoint must be \"module:function\", got %r" % entrypoint)

    manifest = dict(data)
    manifest["id"] = app_id
    manifest["name"] = data["name"].strip()
    manifest["version"] = data["version"].strip()
    manifest["entrypoint"] = entrypoint
    manifest.setdefault("description", "")
    manifest.setdefault("author", "")
    manifest.setdefault("icon", "\U0001f4e6")
    manifest.setdefault("category", "other")
    manifest.setdefault("min_project_os", "0.0.0")
    ui = manifest.get("ui")
    manifest["ui"] = ui if isinstance(ui, dict) else {}
    permissions = manifest.get("permissions")
    manifest["permissions"] = list(permissions) if isinstance(permissions, list) else []
    schema = manifest.get("config_schema")
    manifest["config_schema"] = list(schema) if isinstance(schema, list) else []

    required = _version_tuple(manifest["min_project_os"])
    if required > _version_tuple(__version__):
        raise ManifestError(
            "needs project-os >= %s (this is %s)" % (manifest["min_project_os"], __version__)
        )
    return manifest


# ---------------------------------------------------------------------------
# app-facing objects
# ---------------------------------------------------------------------------
class AppContext(object):
    """Everything an app is allowed to touch, handed to ``setup()``."""

    def __init__(
        self,
        app_id: str,
        manifest: Dict[str, Any],
        app_dir: Path,
        config: AppConfig,
        db: Any,
        bus: Any,
        devices: Any = None,
        core: Optional[Config] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.id = app_id
        self.manifest = manifest
        self.app_dir = Path(app_dir)
        self.data_dir = paths.data_dir(app_id)
        self.logger = logger or logging.getLogger("project_os.app.%s" % app_id)
        self.config = config
        self.db = db
        self.bus = bus
        self.devices = devices
        self.core = core

    @property
    def app_id(self) -> str:
        """Alias for :attr:`id` -- both names show up in app code."""
        return self.id

    @property
    def web_dir(self) -> Path:
        """The app's static panel directory (may not exist)."""
        return self.app_dir / "web"

    @property
    def version(self) -> str:
        return str(self.manifest.get("version", "0.0.0"))

    def emit(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Publish on ``app.<id>.<event>``."""
        if self.bus is None:
            return
        try:
            self.bus.publish_nowait("app.%s.%s" % (self.id, event), data or {})
        except Exception:  # pragma: no cover - the bus must never break an app
            self.logger.debug("emit(%s) failed", event, exc_info=True)

    def base_url(self) -> str:
        """A LAN-reachable ``http://host:port`` for this box.

        ``server.base_url`` wins when the user set one (reverse proxy, mDNS
        name); otherwise it is derived from the primary outbound interface.
        Never raises, including with no network at all.
        """
        configured = ""
        port = 8099
        if self.core is not None:
            try:
                configured = str(self.core.get("server.base_url", "") or "").strip()
                port = int(self.core.get("server.port", 8099) or 8099)
            except Exception:  # pragma: no cover
                configured, port = "", 8099
        if configured:
            return configured.rstrip("/")
        return "http://%s:%d" % (primary_ip(), port)

    def app_url(self, path: str = "") -> str:
        """Absolute URL of one of this app's own API paths."""
        root = "%s/api/apps/%s" % (self.base_url(), self.id)
        if not path:
            return root
        return root + (path if path.startswith("/") else "/" + path)

    def __repr__(self) -> str:
        return "AppContext(%r)" % self.id


class AppInstance(object):
    """What ``setup()`` returns.

    Subclass it, or return any object with the same attributes -- the manager
    duck-types every call so an app can stay dependency-free.
    """

    #: Mounted at ``/api/apps/<id>`` when present.
    router = None  # type: Optional[APIRouter]

    async def start(self) -> None:
        """Begin doing work (timers, connections, scans)."""

    async def stop(self) -> None:
        """Release everything ``start`` acquired. Must be safe to call twice."""

    def status(self) -> Dict[str, Any]:
        """Small dict for the dashboard card."""
        return {}


class ContainerAppInstance(AppInstance):
    """An :class:`AppInstance` backed by a container instead of Python code.

    A directory-based app has ``setup()`` build the instance and asyncio
    supervise it; a container app has no Python to import at all -- the
    catalog's ``container:`` block *is* the app, and the runtime in
    :mod:`project_os.core.containers` is the supervisor. Wrapping it in the
    same start/stop/status shape means the rest of :class:`PluginManager`,
    the dashboard, and the store's "is this installed" question never need to
    know an app is a container.
    """

    def __init__(
        self,
        app_id: str,
        engine: str,
        spec: Dict[str, Any],
        data_dir: Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.app_id = app_id
        self.engine = engine
        self.spec = spec
        self.data_dir = data_dir
        self.log = logger or log
        self.router = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, containers.ensure_running, self.engine, self.app_id, self.spec, self.data_dir
        )

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, containers.stop, self.engine, self.app_id)

    async def remove(self) -> None:
        """Not part of :class:`AppInstance` -- only ``uninstall_container`` calls this."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, containers.remove, self.engine, self.app_id)

    def status(self) -> Dict[str, Any]:
        return containers.status_detail(self.engine, self.app_id)


class LoadedApp(object):
    """The manager's record for one discovered app."""

    def __init__(
        self,
        app_id: str,
        manifest: Dict[str, Any],
        path: Path,
        source: str = "builtin",
        state: str = STATE_STOPPED,
    ) -> None:
        self.id = app_id
        self.manifest = manifest
        self.path = Path(path)
        self.source = source
        self.state = state
        self.error = None  # type: Optional[str]
        self.traceback = None  # type: Optional[str]
        self.instance = None  # type: Optional[Any]
        self.context = None  # type: Optional[AppContext]
        self.module = None  # type: Optional[Any]
        self.started_at = None  # type: Optional[str]

    @property
    def web_dir(self) -> Path:
        return self.path / "web"

    @property
    def router(self) -> Optional[APIRouter]:
        return getattr(self.instance, "router", None)

    def to_dict(self) -> Dict[str, Any]:
        manifest = dict(self.manifest)
        return {
            "id": self.id,
            "name": manifest.get("name", self.id),
            "version": manifest.get("version", "0.0.0"),
            "description": manifest.get("description", ""),
            "author": manifest.get("author", ""),
            "icon": manifest.get("icon", "\U0001f4e6"),
            "category": manifest.get("category", "other"),
            "permissions": manifest.get("permissions", []),
            "config_schema": manifest.get("config_schema", []),
            "ui": manifest.get("ui", {}),
            "manifest": manifest,
            "source": self.source,
            "path": str(self.path),
            "state": self.state,
            "enabled": self.state != STATE_DISABLED,
            "error": self.error,
            "traceback": self.traceback,
            "started_at": self.started_at,
            "has_ui": self.web_dir.is_dir(),
            "has_api": self.router is not None,
        }

    def __repr__(self) -> str:
        return "LoadedApp(%r, state=%r)" % (self.id, self.state)


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------
class PluginManager(object):
    """Discovers, loads, supervises and exposes apps."""

    def __init__(
        self,
        config: Config,
        db: Any,
        bus: Any,
        devices: Any = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        self.devices = devices
        self.log = logger or log
        self._apps = {}  # type: Dict[str, LoadedApp]
        self._lock = None  # type: Optional[asyncio.Lock]
        self._router_hook = None  # type: Optional[Callable[[str, APIRouter], None]]

    # -- plumbing --------------------------------------------------------
    def _get_lock(self) -> asyncio.Lock:
        # Created lazily: on 3.9 an asyncio.Lock binds to the loop that exists
        # when it is constructed, and the manager may be built before one runs.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def set_router_hook(self, hook: Optional[Callable[[str, APIRouter], None]]) -> None:
        """Called with ``(app_id, router)`` as soon as an app exposes an API.

        ``main`` uses it to mount routers of apps that are enabled *after*
        boot, so the app system does not need to know about FastAPI.
        """
        self._router_hook = hook

    def _emit_state(self, record: LoadedApp) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish_nowait(
                "app.state",
                {
                    "id": record.id,
                    "state": record.state,
                    "error": record.error,
                    "name": record.manifest.get("name", record.id),
                },
            )
        except Exception:  # pragma: no cover
            self.log.debug("publishing app.state failed", exc_info=True)

    def _set_state(self, record: LoadedApp, state: str) -> None:
        if record.state == state:
            return
        record.state = state
        self._emit_state(record)

    def _fail(self, record: LoadedApp, exc: BaseException, phase: str) -> None:
        record.error = "%s: %s" % (type(exc).__name__, exc)
        record.traceback = _short_traceback(exc)
        self.log.error("app %s failed to %s: %s", record.id, phase, record.error)
        self.log.debug("%s", record.traceback)
        self._set_state(record, STATE_ERROR)

    # -- discovery -------------------------------------------------------
    def _candidate_dirs(self) -> List[Tuple[str, Path]]:
        # Order matters: user apps are scanned last so they win on id collision.
        return [("builtin", paths.builtin_apps_dir()), ("user", paths.apps_dir())]

    def discover(self) -> Dict[str, LoadedApp]:
        """Scan both app directories and build a record per app.

        Apps whose manifest is unusable get a record in the ``error`` state so
        the user can see *why* in the UI instead of the app silently vanishing.
        """
        found = {}  # type: Dict[str, LoadedApp]
        for source, directory in self._candidate_dirs():
            try:
                if not directory.is_dir():
                    continue
                entries = sorted(directory.iterdir())
            except OSError as exc:  # pragma: no cover
                self.log.warning("cannot read %s: %s", directory, exc)
                continue
            for entry in entries:
                if not entry.is_dir() or entry.name.startswith((".", "_")):
                    continue
                manifest_file = entry / "manifest.json"
                if not manifest_file.is_file():
                    continue
                try:
                    raw = json.loads(manifest_file.read_text("utf-8"))
                    manifest = validate_manifest(raw, expected_id=entry.name)
                except (OSError, ValueError) as exc:
                    self.log.error("ignoring app at %s: %s", entry, exc)
                    broken = LoadedApp(
                        entry.name,
                        {
                            "id": entry.name,
                            "name": entry.name,
                            "version": "0.0.0",
                            "entrypoint": "",
                            "description": "This app has an invalid manifest.json.",
                            "icon": "warning",
                        },
                        entry,
                        source,
                        state=STATE_ERROR,
                    )
                    broken.error = str(exc)
                    found[entry.name] = broken
                    continue
                found[manifest["id"]] = LoadedApp(manifest["id"], manifest, entry, source)
        return found

    def enabled_ids(self) -> Optional[List[str]]:
        """``apps.enabled`` as written, or ``None`` when the key is absent.

        An empty list means *empty*. It used to mean "not configured", and the
        fallback for "not configured" was to run every bundled app -- so a fresh
        machine booted with everything on, which is the opposite of the whole
        idea:

            "ele seria um sistema base, sem nada por padrao, mas dai vc vai
             conectando coisas nele. igual o homeassist, vc baixa e n tem nada,
             dai vc vai na loja de app e baixa oq vc quer"

        It is also how a WhatsApp bot nobody installed ended up running on his
        machine. ``None`` is kept for a config file that predates the key.
        """
        raw = self.config.get("apps.enabled", None)
        if not isinstance(raw, list):
            return None
        return [str(item) for item in raw]

    def is_enabled(self, app_id: str, source: str = "builtin") -> bool:
        enabled = self.enabled_ids()
        if enabled is None:
            return source == "builtin"
        return app_id in enabled

    # -- loading ---------------------------------------------------------
    def _import_entrypoint(self, record: LoadedApp) -> Any:
        """Import the module named by the manifest entrypoint."""
        module_part = record.manifest["entrypoint"].partition(":")[0]
        if record.source == "builtin":
            module_name = "project_os.apps.%s.%s" % (record.id, module_part)
            return importlib.import_module(module_name)

        # User-installed app: give it a synthetic package rooted at its own
        # directory so `from .library import x` inside the app resolves.
        package_name = "project_os_app_%s" % record.id.replace("-", "_")
        package = sys.modules.get(package_name)
        if package is None:
            spec = importlib.machinery.ModuleSpec(package_name, None, is_package=True)
            package = importlib.util.module_from_spec(spec)
            package.__path__ = [str(record.path)]  # type: ignore[attr-defined]
            sys.modules[package_name] = package
        else:
            package.__path__ = [str(record.path)]  # type: ignore[attr-defined]

        module_name = "%s.%s" % (package_name, module_part)
        cached = sys.modules.get(module_name)
        if cached is not None:
            return cached

        relative = module_part.replace(".", "/")
        candidates = [record.path / (relative + ".py"), record.path / relative / "__init__.py"]
        for candidate in candidates:
            if candidate.is_file():
                is_package = candidate.name == "__init__.py"
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    str(candidate),
                    submodule_search_locations=[str(candidate.parent)] if is_package else None,
                )
                if spec is None or spec.loader is None:  # pragma: no cover
                    raise ImportError("cannot build a loader for %s" % candidate)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop(module_name, None)
                    raise
                return module
        raise ImportError(
            "entrypoint module %r not found in %s" % (module_part, record.path)
        )

    def _apply_schema_defaults(self, record: LoadedApp, app_config: AppConfig) -> None:
        for entry in record.manifest.get("config_schema", []):
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if key and "default" in entry:
                app_config.setdefault(str(key), entry["default"])

    async def _instantiate(self, record: LoadedApp) -> Any:
        loop = asyncio.get_running_loop()
        # Importing runs arbitrary app code and touches the filesystem.
        module = await loop.run_in_executor(None, self._import_entrypoint, record)
        record.module = module

        func_name = record.manifest["entrypoint"].partition(":")[2]
        setup = getattr(module, func_name, None)
        if not callable(setup):
            raise ImportError(
                "%s has no callable %r" % (getattr(module, "__name__", "?"), func_name)
            )

        app_config = self.config.app(record.id)
        self._apply_schema_defaults(record, app_config)
        context = AppContext(
            app_id=record.id,
            manifest=record.manifest,
            app_dir=record.path,
            config=app_config,
            db=self.db,
            bus=self.bus,
            devices=self.devices,
            core=self.config,
        )
        record.context = context

        instance = await _maybe_await(setup(context))
        if instance is None:
            raise TypeError("setup() returned None, expected an AppInstance")
        router = getattr(instance, "router", None)
        if router is not None and self._router_hook is not None:
            self._router_hook(record.id, router)
        return instance

    async def load_all(self) -> List[Dict[str, Any]]:
        """Discover every app, then start the enabled ones.

        Returns the same shape as :meth:`list_apps`. Never raises for an app's
        own mistakes.
        """
        discovered = self.discover()
        for app_id, record in discovered.items():
            existing = self._apps.get(app_id)
            if existing is not None and existing.instance is not None:
                # Already live (a reload during runtime): keep the instance.
                existing.manifest = record.manifest
                continue
            self._apps[app_id] = record

        autostart = bool(self.config.get("apps.autostart", True))
        for app_id in sorted(self._apps):
            record = self._apps[app_id]
            if not record.manifest.get("entrypoint"):
                continue  # broken manifest, already reported by discover()
            if not self.is_enabled(app_id, record.source):
                self._set_state(record, STATE_DISABLED)
                continue
            if record.state == STATE_DISABLED:
                self._set_state(record, STATE_STOPPED)
            if autostart:
                await self.start(app_id)
        running = [a for a in self._apps.values() if a.state == STATE_RUNNING]
        self.log.info(
            "apps: %d discovered, %d running", len(self._apps), len(running)
        )
        return self.list_apps()

    def has(self, app_id: str) -> bool:
        """Whether this app exists as code on this machine.

        The catalog lists a few builtins that are planned and not written yet;
        the store asks this before offering an Install button, so a card can say
        "not ready" instead of the install failing with "No app called 'kasa'".
        """
        return app_id in self._apps

    # -- lifecycle -------------------------------------------------------
    def _require(self, app_id: str) -> LoadedApp:
        record = self._apps.get(app_id)
        if record is None:
            raise ApiError(404, "app_not_found", "No app called %r." % app_id)
        return record

    async def start(self, app_id: str) -> Dict[str, Any]:
        """Load (if needed) and start one app. Failure is recorded, not raised."""
        async with self._get_lock():
            record = self._require(app_id)
            if record.state == STATE_RUNNING or not record.manifest.get("entrypoint"):
                return record.to_dict()
            self._set_state(record, STATE_STARTING)
            try:
                if record.instance is None:
                    record.instance = await self._instantiate(record)
                start = getattr(record.instance, "start", None)
                if callable(start):
                    await _maybe_await(start())
            except asyncio.CancelledError:
                self._set_state(record, STATE_STOPPED)
                raise
            except BaseException as exc:
                self._fail(record, exc, "start")
                return record.to_dict()
            record.error = None
            record.traceback = None
            record.started_at = _utcnow_iso()
            self._set_state(record, STATE_RUNNING)
            self.log.info("app %s running", record.id)
            return record.to_dict()

    async def stop(self, app_id: str) -> Dict[str, Any]:
        """Stop one app. The instance is kept so its routes stay mounted."""
        async with self._get_lock():
            record = self._require(app_id)
            instance = record.instance
            if instance is not None:
                stop = getattr(instance, "stop", None)
                if callable(stop):
                    try:
                        await _maybe_await(stop())
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        self._fail(record, exc, "stop")
                        return record.to_dict()
            record.started_at = None
            self._set_state(record, STATE_STOPPED)
            return record.to_dict()

    async def restart(self, app_id: str) -> Dict[str, Any]:
        await self.stop(app_id)
        return await self.start(app_id)

    async def enable(self, app_id: str) -> Dict[str, Any]:
        """Add the app to ``apps.enabled``, persist, and start it."""
        record = self._require(app_id)
        enabled = self.enabled_ids()
        if enabled is None:
            enabled = [a.id for a in self._apps.values() if a.source == "builtin"]
        if app_id not in enabled:
            enabled.append(app_id)
            self.config.set("apps.enabled", sorted(set(enabled)))
            self.config.save()
        if record.state == STATE_DISABLED:
            self._set_state(record, STATE_STOPPED)
        return await self.start(app_id)

    async def disable(self, app_id: str) -> Dict[str, Any]:
        """Stop the app and remove it from ``apps.enabled``."""
        record = self._require(app_id)
        await self.stop(app_id)
        enabled = self.enabled_ids()
        if enabled is None:
            enabled = [a.id for a in self._apps.values() if a.source == "builtin"]
        remaining = [item for item in enabled if item != app_id]
        self.config.set("apps.enabled", sorted(set(remaining)))
        self.config.save()
        self._set_state(record, STATE_DISABLED)
        return record.to_dict()

    # -- container apps ---------------------------------------------------
    async def install_container(
        self, app_id: str, entry: Dict[str, Any], spec: Dict[str, Any], engine: str
    ) -> Dict[str, Any]:
        """Register a catalog app backed by a container, then start it.

        The catalog entry stands in for the manifest.json a directory-based
        app would have. Once the record exists in ``self._apps``, ``enable()``
        does exactly what it does for any other app -- persist it, start it,
        and hand back its state -- so a container's failure to pull or start
        shows up the same way a builtin's failure to start would: recorded on
        the app, not raised out of the endpoint.
        """
        data_dir = paths.data_dir(app_id)
        instance = ContainerAppInstance(
            app_id, engine, spec, data_dir, logger=logging.getLogger("project_os.app.%s" % app_id)
        )
        manifest = {
            "id": app_id,
            "name": entry.get("name", app_id),
            "version": str(entry.get("version") or "0.0.0"),
            "description": entry.get("description", ""),
            "icon": entry.get("icon", "box"),
            "category": entry.get("category", "other"),
            "permissions": [],
            "config_schema": [],
            "ui": {},
            # start()/discover_and_start_enabled() both treat a falsy
            # entrypoint as "nothing to start" -- true for a manifest.json app
            # with no code to import, but not for a container app, which very
            # much has something to start. This value is never parsed as a
            # module:function reference: record.instance is set below, before
            # enable() runs, so _instantiate() (the only reader of
            # "entrypoint") is always skipped for a container app.
            "entrypoint": "container",
        }
        record = self._apps.get(app_id)
        if record is None:
            record = LoadedApp(app_id, manifest, data_dir, source="container", state=STATE_STOPPED)
            self._apps[app_id] = record
        else:
            record.manifest = manifest
        record.instance = instance
        return await self.enable(app_id)

    async def uninstall_container(self, app_id: str) -> Dict[str, Any]:
        """Stop, ``rm``, and forget a container app.

        A directory app's uninstall deletes its folder; this is the container
        equivalent. ``disable()`` alone would leave the container sitting
        there stopped and the record parked as "disabled" -- fine for a
        builtin you might re-enable, wrong for something the user asked
        removed.
        """
        record = self._apps.get(app_id)
        if record is None:
            raise ApiError(404, "app_not_found", "No app called %r." % app_id)
        await self.stop(app_id)
        remover = getattr(record.instance, "remove", None)
        if callable(remover):
            await _maybe_await(remover())
        enabled = self.enabled_ids()
        if enabled is not None:
            remaining = [item for item in enabled if item != app_id]
            self.config.set("apps.enabled", sorted(set(remaining)))
            self.config.save()
        self._apps.pop(app_id, None)
        return {"id": app_id, "name": record.manifest.get("name", app_id), "installed": False}

    async def stop_all(self) -> None:
        """Shutdown path: stop everything, complain about nothing."""
        for app_id in sorted(self._apps):
            record = self._apps[app_id]
            if record.instance is None:
                continue
            stop = getattr(record.instance, "stop", None)
            if not callable(stop):
                continue
            try:
                await _maybe_await(stop())
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # pragma: no cover
                self.log.warning("app %s did not stop cleanly: %s", app_id, exc)
            record.state = STATE_STOPPED
            record.started_at = None

    # -- queries ---------------------------------------------------------
    def get(self, app_id: str) -> Optional[LoadedApp]:
        return self._apps.get(app_id)

    def info(self, app_id: str) -> Optional[Dict[str, Any]]:
        """One app as a dict, status included (or ``None`` if unknown)."""
        record = self._apps.get(app_id)
        if record is None:
            return None
        return self._describe(record)

    def _describe(self, record: LoadedApp) -> Dict[str, Any]:
        data = record.to_dict()
        data["status"] = self.status_of(record)
        return data

    def status_of(self, record: LoadedApp) -> Dict[str, Any]:
        instance = record.instance
        if instance is None:
            return {}
        status = getattr(instance, "status", None)
        if not callable(status):
            return {}
        try:
            result = status()
        except Exception as exc:
            self.log.warning("status() failed for app %s: %s", record.id, exc)
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
        return result if isinstance(result, dict) else {"value": result}

    def list_apps(self) -> List[Dict[str, Any]]:
        return [self._describe(self._apps[app_id]) for app_id in sorted(self._apps)]

    def routers(self) -> List[Tuple[str, APIRouter]]:
        """``(app_id, router)`` for every app that exposes an API."""
        out = []  # type: List[Tuple[str, APIRouter]]
        for app_id in sorted(self._apps):
            router = self._apps[app_id].router
            if router is not None:
                out.append((app_id, router))
        return out

    def ui_path(self, app_id: str) -> Optional[Path]:
        """The app's ``web/`` directory, when it ships one."""
        record = self._apps.get(app_id)
        if record is None:
            return None
        web = record.web_dir
        return web if web.is_dir() else None

    def app_config(self, app_id: str) -> AppConfig:
        return self.config.app(app_id)

    def __contains__(self, app_id: object) -> bool:
        return app_id in self._apps

    def __len__(self) -> int:
        return len(self._apps)


__all__ = [
    "APP_ID_RE",
    "AppConfig",
    "AppContext",
    "AppInstance",
    "ContainerAppInstance",
    "LoadedApp",
    "ManifestError",
    "PluginManager",
    "STATE_DISABLED",
    "STATE_ERROR",
    "STATE_RUNNING",
    "STATE_STARTING",
    "STATE_STOPPED",
    "primary_ip",
    "validate_manifest",
]
