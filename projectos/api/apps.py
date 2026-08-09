"""Installed apps: what is there, and starting and stopping it.

This router talks about apps that are *already installed*. Installing one is the
store's job (:mod:`projectos.api.store`), and the split matters: after the
concept change a clean ProjectOS has nothing installed at all, so this endpoint
returning an empty list is the normal first-run state, not a failure.

The UI-asset route ``/apps/{app_id}/ui/{path}`` is deliberately absent -- it is
owned by :mod:`projectos.main`, which guards it against path traversal.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from projectos import auth
from projectos.db import Database
from projectos.errors import ApiError
from projectos.main import get_db, get_plugins

log = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["apps"])

ACTIONS = ("start", "stop", "restart", "enable", "disable")


class AppAction(BaseModel):
    action: str = Field(..., pattern="^(start|stop|restart|enable|disable)$")


def _require(plugins: Any, app_id: str) -> Any:
    record = plugins.get(app_id)
    if record is None:
        raise ApiError(404, "app_not_found", "No app called %r is installed." % app_id)
    return record


@router.get("")
async def list_apps(
    state: Optional[str] = Query(None, description="running | stopped | error | disabled"),
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    apps = plugins.list_apps()
    if state:
        apps = [item for item in apps if item.get("state") == state]
    counts = {}  # type: Dict[str, int]
    for item in plugins.list_apps():
        key = item.get("state") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return {"apps": apps, "count": len(apps), "by_state": counts}


@router.get("/{app_id}")
async def get_app(
    app_id: str,
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    record = _require(plugins, app_id)
    payload = plugins.info(app_id) or {}
    # Nested, never merged: an app's status() is its own vocabulary. BirdTunes
    # reports the *player's* state and name, and flattening that into the record
    # made a stopped Chromecast read as "the app failed to start" -- the whole
    # panel was replaced by an error page you could not even change the output
    # from. list_apps() has always nested it; this route now agrees.
    payload["status"] = plugins.status_of(record)
    payload["has_ui"] = plugins.ui_path(app_id) is not None
    return payload


@router.post("/{app_id}/action")
async def act(
    app_id: str,
    payload: AppAction,
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """One endpoint for all five verbs.

    They differ in one word and share every guard, so five near-identical routes
    would be five places to forget the same check.
    """
    _require(plugins, app_id)
    method = getattr(plugins, payload.action, None)
    if method is None:  # pragma: no cover - ACTIONS is closed
        raise ApiError(400, "unknown_action", "No such action %r." % payload.action)
    result = await method(app_id)
    log.info("app %s: %s (by %s)", app_id, payload.action, user.get("username"))
    return {"ok": True, "action": payload.action, "app": result}


@router.get("/{app_id}/logs")
async def app_logs(
    app_id: str,
    limit: int = Query(200, ge=1, le=2000),
    plugins: Any = Depends(get_plugins),
    db: Database = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """An app's own log lines.

    Deliberately works for an app in the ``error`` state, which is exactly when
    someone needs it -- so this route does not require the app to be loaded.
    """
    record = plugins.get(app_id)
    lines = db.recent_log(limit=limit, source="app.%s" % app_id)
    if not lines and record is None:
        raise ApiError(404, "app_not_found", "No app called %r is installed." % app_id)
    status = plugins.status_of(record) if record is not None else {"state": "missing"}
    return {"id": app_id, "state": status.get("state"), "lines": lines}


class AppConfigPatch(BaseModel):
    """Dotted paths relative to the app, e.g. ``{"output.device_id": "abc"}``.

    Same shape as ``PUT /api/settings`` takes inside ``values``, because a panel
    author should not have to learn a second one.
    """

    class Config:
        extra = "allow"


@router.get("/{app_id}/config")
async def get_app_config(
    app_id: str,
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """An app's own settings, secrets masked.

    The frontend's ``ctx.config`` helper has always called this route; until now
    nothing answered it, so every panel that asked for its own configuration got
    a 404 and silently rendered as though nothing was set. BirdTunes showing
    "Speaker: not chosen" next to a configured Chromecast was this.
    """
    _require(plugins, app_id)
    config = plugins.app_config(app_id)
    return {"id": app_id, "config": config.as_dict()}


@router.put("/{app_id}/config")
async def put_app_config(
    app_id: str,
    body: Dict[str, Any],
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    from projectos.config import is_redacted

    _require(plugins, app_id)
    config = plugins.app_config(app_id)
    changed = []  # type: List[str]
    for path, value in (body or {}).items():
        if not isinstance(path, str) or not path.strip():
            raise ApiError(400, "bad_request", "Setting names must be non-empty strings.")
        # A masked secret read back and sent again is not a change; writing it
        # would replace the real value with the mask.
        if is_redacted(value):
            continue
        config.set(path, value)
        changed.append(path)
    if changed:
        config.save()
    return {"id": app_id, "changed": changed, "config": config.as_dict()}


@router.get("/{app_id}/status")
async def app_status(
    app_id: str,
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return plugins.status_of(_require(plugins, app_id))


__all__ = ["router"]
