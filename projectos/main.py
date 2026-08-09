"""The FastAPI application: wiring, lifespan, static frontend.

``create_app()`` builds everything; the lifespan owns the order in which the
subsystems come up and, more importantly, the order in which they go down.
Shared objects live on ``app.state`` and are reachable from any route through
the dependencies at the bottom of this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import logging.handlers
import mimetypes
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Mount
from starlette.types import Scope

from projectos import __version__, paths
from projectos.config import Config, load_config, set_config
from projectos.core.discovery import DeviceRegistry
from projectos.core.plugins import PluginManager
from projectos.core.suggestions import SuggestionEngine
from projectos.db import Database
from projectos.errors import ApiError, install_error_handlers
from projectos.events import EventBus

log = logging.getLogger("projectos")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3
STATS_INTERVAL_SECONDS = 5.0
DISCOVERY_FALLBACK_INTERVAL = 300.0


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
class EventLogHandler(logging.Handler):
    """Mirrors log records into the event bus (topic ``log``) and the database.

    Anything that goes wrong in here is swallowed: a logging handler that
    raises would take down whatever was merely trying to report a problem.
    """

    def __init__(self, bus: EventBus, db: Optional[Database], cap: int = 2000) -> None:
        logging.Handler.__init__(self, level=logging.INFO)
        self.bus = bus
        self.db = db
        self.cap = int(cap)
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            try:
                message = record.getMessage()
            except Exception:  # pragma: no cover - bad format string
                message = str(record.msg)
            if record.exc_info:
                message = "%s\n%s" % (message, self.format_exception(record))
            payload = {
                "level": record.levelname,
                "source": record.name,
                "message": message,
            }
            if self.bus is not None:
                self.bus.publish_nowait("log", payload)
            if self.db is not None:
                self.db.add_log(record.levelname, record.name, message, cap=self.cap)
        except Exception:
            pass
        finally:
            self._local.busy = False

    def format_exception(self, record: logging.LogRecord) -> str:
        try:
            return logging.Formatter().formatException(record.exc_info)
        except Exception:  # pragma: no cover
            return ""


def _tagged(handler: logging.Handler) -> logging.Handler:
    setattr(handler, "_projectos_handler", True)
    return handler


def _remove_our_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, "_projectos_handler", False):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()


def configure_logging(config: Config) -> None:
    """stderr + a rotating file under PROJECTOS_HOME. Idempotent."""
    level_name = str(config.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):  # pragma: no cover - defensive
        level = logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    _remove_our_handlers(root)

    formatter = logging.Formatter(LOG_FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(_tagged(stream))

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            str(paths.log_file()),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(_tagged(file_handler))
    except OSError as exc:
        root.warning("no log file (%s); logging to stderr only", exc)


def attach_event_logging(config: Config, bus: EventBus, db: Database) -> EventLogHandler:
    handler = EventLogHandler(bus, db, cap=int(config.get("logging.retain_lines", 2000) or 2000))
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    _tagged(handler)
    logging.getLogger().addHandler(handler)
    return handler


def detach_event_logging(handler: Optional[logging.Handler]) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    with contextlib.suppress(Exception):
        handler.close()


# ---------------------------------------------------------------------------
# background tasks
# ---------------------------------------------------------------------------
_STATS_FUNCTION_NAMES = ("stats", "get_stats", "collect_stats", "system_stats", "snapshot")
_stats_callable = None  # type: Optional[Callable[[], Any]]
_stats_resolved = False
_process_start = time.time()


def _fallback_stats() -> Dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "uptime_seconds": round(time.time() - _process_start, 1),
        "available": False,
    }


def _resolve_stats_callable() -> Callable[[], Any]:
    """Find whatever ``core.sysinfo`` calls its stats function.

    That module is written by another part of the build; we bind to it once and
    degrade to a minimal payload if it is missing or shaped differently.
    """
    global _stats_callable, _stats_resolved
    if _stats_resolved and _stats_callable is not None:
        return _stats_callable
    _stats_resolved = True
    try:
        from projectos.core import sysinfo
    except Exception as exc:
        log.warning("system stats unavailable (%s)", exc)
        _stats_callable = _fallback_stats
        return _stats_callable
    for name in _STATS_FUNCTION_NAMES:
        candidate = getattr(sysinfo, name, None)
        if callable(candidate):
            _stats_callable = candidate
            return candidate
    log.warning("projectos.core.sysinfo exposes no stats function")
    _stats_callable = _fallback_stats
    return _stats_callable


async def collect_stats() -> Dict[str, Any]:
    func = _resolve_stats_callable()
    if asyncio.iscoroutinefunction(func):
        result = await func()
    else:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, func)
        if inspect.isawaitable(result):
            result = await result
    return result if isinstance(result, dict) else {"value": result}


async def _stats_loop(bus: EventBus, interval: float = STATS_INTERVAL_SECONDS) -> None:
    while True:
        try:
            bus.publish_nowait("system.stats", await collect_stats())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("system.stats collection failed", exc_info=True)
        await asyncio.sleep(interval)


async def _discovery_loop(devices: Any, interval: float) -> None:
    """Fallback periodic scan for a registry that does not run its own."""
    while True:
        try:
            result = devices.scan()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("device scan failed", exc_info=True)
        await asyncio.sleep(interval)


def _construct(factory: Any, **kwargs: Any) -> Any:
    """Build a collaborator, passing only the arguments it actually declares.

    Keeps the wiring honest across modules that are developed in parallel: a
    registry that takes ``(config, db, bus)`` and one that takes ``(db, bus)``
    both get built correctly.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return factory(**kwargs)
    parameters = signature.parameters
    if any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        selected = dict(kwargs)
    else:
        selected = dict((k, v) for k, v in kwargs.items() if k in parameters)
    try:
        return factory(**selected)
    except TypeError:
        ordered = [kwargs[name] for name in parameters if name in kwargs]
        return factory(*ordered)


async def _cancel(tasks: List[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _maybe_call(obj: Any, *names: str) -> None:
    for name in names:
        func = getattr(obj, name, None)
        if callable(func):
            try:
                result = func()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.warning("%s.%s() failed during shutdown: %s", type(obj).__name__, name, exc)
            return


# ---------------------------------------------------------------------------
# static frontend
# ---------------------------------------------------------------------------
class SpaStaticFiles(StaticFiles):
    """Static files with an index.html fallback for client-side routes.

    Two things never fall back, and both matter: anything under ``/api`` (an
    unknown endpoint must answer with the JSON error contract, not with HTML a
    fetch() would choke on) and paths that look like a file, so a mistyped
    ``/lib/dom.js`` 404s instead of quietly returning HTML to a script tag.

    Every response also carries ``Cache-Control: no-cache``. The filenames are
    unversioned -- ``/style.css`` is always ``/style.css`` -- and without an
    explicit header a browser is free to guess a freshness lifetime, which after
    an upgrade means old CSS and old modules against a new API. ``no-cache`` is
    not "do not cache": the file is still stored and still revalidated with the
    ETag, so an unchanged asset costs a 304 and nothing more.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
            response.headers.setdefault("cache-control", "no-cache")
            return response
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if path == "api" or path.startswith("api/"):
                raise
            if "." in path.rsplit("/", 1)[-1]:
                raise
            fallback = await super().get_response("index.html", scope)
            fallback.headers.setdefault("cache-control", "no-cache")
            return fallback


def _safe_child(root: Path, relative: str) -> Optional[Path]:
    """Resolve ``relative`` under ``root``, refusing to escape it."""
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        return None
    return candidate


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------
def _from_state(request: Request, name: str, label: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise ApiError(503, "not_ready", "%s is not available yet." % label)
    return value


def get_config(request: Request) -> Config:
    return _from_state(request, "config", "Configuration")


def get_db(request: Request) -> Database:
    return _from_state(request, "db", "The database")


def get_bus(request: Request) -> EventBus:
    return _from_state(request, "bus", "The event bus")


def get_devices(request: Request) -> Any:
    return _from_state(request, "devices", "Device discovery")


def get_suggestions(request: Request) -> Any:
    return _from_state(request, "suggestions", "The suggestion engine")


def get_plugins(request: Request) -> PluginManager:
    return _from_state(request, "plugins", "The app manager")


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state
    config = load_config()
    set_config(config)
    state.config = config
    configure_logging(config)
    log.info("ProjectOS %s starting (home=%s)", __version__, paths.home())

    db = Database()
    db.connect()
    db.migrate()
    state.db = db

    bus = EventBus()
    bus.set_loop(asyncio.get_running_loop())
    state.bus = bus

    log_handler = attach_event_logging(config, bus, db)
    tasks = []  # type: List[asyncio.Task]

    devices = None
    try:
        devices = _construct(DeviceRegistry, config=config, db=db, bus=bus)
    except Exception:
        log.exception("device discovery is unavailable")
    state.devices = devices

    if devices is not None and bool(config.get("discovery.enabled", True)):
        interval = float(
            config.get("discovery.interval_seconds", DISCOVERY_FALLBACK_INTERVAL)
            or DISCOVERY_FALLBACK_INTERVAL
        )
        starter = getattr(devices, "start", None)
        if callable(starter):
            try:
                result = starter()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("could not start the device registry")
        else:
            tasks.append(asyncio.create_task(_discovery_loop(devices, interval), name="discovery"))

    suggestions = None
    try:
        suggestions = _construct(
            SuggestionEngine, config=config, db=db, bus=bus, devices=devices
        )
    except Exception:
        log.exception("the suggestion engine is unavailable")
    state.suggestions = suggestions

    plugins = PluginManager(config=config, db=db, bus=bus, devices=devices)
    plugins.set_router_hook(_make_router_mounter(app))
    state.plugins = plugins
    try:
        await plugins.load_all()
    except Exception:
        log.exception("loading apps failed")

    tasks.append(asyncio.create_task(_stats_loop(bus), name="system-stats"))
    state.tasks = tasks
    state.started_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    log.info("ProjectOS ready on port %s", config.get("server.port", 8099))

    try:
        yield
    finally:
        log.info("ProjectOS shutting down")
        await _cancel(tasks)
        with contextlib.suppress(Exception):
            await plugins.stop_all()
        if devices is not None:
            await _maybe_call(devices, "stop", "close", "aclose", "shutdown")
        if suggestions is not None:
            await _maybe_call(suggestions, "stop", "close")
        with contextlib.suppress(Exception):
            bus.close()
        detach_event_logging(log_handler)
        with contextlib.suppress(Exception):
            db.close()
        set_config(None)


def _make_router_mounter(app: FastAPI) -> Callable[[str, APIRouter], None]:
    """Mount an app router at ``/api/apps/<id>``, once, whenever it appears.

    Apps come up during the lifespan, i.e. after the static frontend has been
    mounted at ``/`` -- and that mount matches every path. So each new router
    is followed by pushing the catch-all back to the end of the route list.
    """
    mounted = set()  # type: Set[str]

    def mount(app_id: str, router: APIRouter) -> None:
        if app_id in mounted:
            return
        mounted.add(app_id)
        routes = app.router.routes
        before = len(routes)
        app.include_router(
            router, prefix="/api/apps/%s" % app_id, tags=["app:%s" % app_id]
        )
        # An app's own routes go to the front. The generic ones in api/apps.py
        # include /apps/{app_id}/config, which would otherwise shadow an app that
        # implements its own -- and the app's version usually does more than
        # write a value (the WhatsApp bot rebuilds its provider on save). First
        # match wins in Starlette, so "more specific first" has to be literal.
        added = routes[before:]
        del routes[before:]
        routes[0:0] = added
        for route in [r for r in routes if isinstance(r, Mount) and r.path in ("", "/")]:
            routes.remove(route)
            routes.append(route)
        # Routes added after startup must invalidate the cached schema.
        app.openapi_schema = None
        log.info("mounted API for app %s at /api/apps/%s", app_id, app_id)

    return mount


def create_app() -> FastAPI:
    """Build the ProjectOS application."""
    app = FastAPI(
        title="ProjectOS",
        version=__version__,
        description="The system layer of a Raspberry Pi, driven from a browser.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    install_error_handlers(app)

    @app.get("/api/apps/{app_id}/ui/{file_path:path}", include_in_schema=False)
    async def app_ui(app_id: str, file_path: str, request: Request) -> Response:
        """Serve an app's own ``web/`` directory."""
        plugins = getattr(request.app.state, "plugins", None)
        root = plugins.ui_path(app_id) if plugins is not None else None
        if root is None:
            raise ApiError(404, "app_ui_not_found", "App %r has no UI files." % app_id)
        target = _safe_child(root, file_path)
        if target is None:
            raise ApiError(403, "forbidden_path", "That path is outside the app.")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise ApiError(404, "not_found", "No such file: %s" % file_path)
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # Same reasoning as SpaStaticFiles: unversioned names, so revalidate.
        # Upgrading an app while a browser kept its old panel.js is a real way
        # to end up with a UI calling routes that no longer exist.
        return FileResponse(
            str(target), media_type=media_type, headers={"cache-control": "no-cache"}
        )

    # The aggregate API router is imported here rather than at module import
    # time: api submodules depend on the dependencies defined above, and a
    # top-level import would make that a cycle.
    try:
        from projectos.api import api_router

        app.include_router(api_router, prefix="/api")
    except Exception:
        log.exception("the API router could not be built; only /api/health will answer")

    @app.get("/api/health", include_in_schema=False)
    async def health(request: Request) -> JSONResponse:
        """Public liveness probe (a real one may be provided by api.system)."""
        db = getattr(request.app.state, "db", None)
        pending = None
        if db is not None:
            from projectos.auth import setup_required

            with contextlib.suppress(Exception):
                pending = setup_required(db)
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "setup_required": pending,
                "started_at": getattr(request.app.state, "started_at", None),
            }
        )

    web_root = paths.web_dir()
    if web_root.is_dir():
        app.mount(
            "/",
            SpaStaticFiles(directory=str(web_root), html=True, check_dir=False),
            name="web",
        )
    else:
        log.warning("frontend directory %s is missing; serving the API only", web_root)

        @app.get("/", include_in_schema=False)
        async def no_frontend() -> PlainTextResponse:
            return PlainTextResponse(
                "ProjectOS %s is running, but the web/ directory was not found at %s.\n"
                "The API is available under /api.\n" % (__version__, web_root),
                status_code=503,
            )

    return app


__all__ = [
    "EventLogHandler",
    "SpaStaticFiles",
    "attach_event_logging",
    "collect_stats",
    "configure_logging",
    "create_app",
    "get_bus",
    "get_config",
    "get_db",
    "get_devices",
    "get_plugins",
    "get_suggestions",
    "lifespan",
]
