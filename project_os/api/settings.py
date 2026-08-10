"""Reading and changing the configuration.

The one rule that shapes this file: **secrets go out redacted and come back
ignored.** ``GET /api/settings`` replaces every token and password with
``********``, and a ``PUT`` that contains that exact placeholder leaves the
stored value alone. Without the second half, opening the settings page and
pressing Save would quietly overwrite the Home Assistant token with eight
asterisks -- which is the kind of bug people spend an evening on.

Layers are exposed too (``defaults`` / ``file`` / ``env``), because
``PROJECT_OS__SERVER__PORT`` silently winning over the value in the YAML is
otherwise indistinguishable from project-os ignoring the setting.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from project_os import auth, config as config_module
from project_os.errors import ApiError
from project_os.main import get_config, get_plugins

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

#: Changing these does nothing until the process restarts. The UI says so rather
#: than letting someone move the port and wonder why the page still answers.
RESTART_REQUIRED = (
    "server.host",
    "server.port",
    "logging.level",
    "security.auth_enabled",
)

#: Settings project-os refuses to write from the browser. ``file_roots`` widens
#: what the file manager can reach, so it is deliberately a config-file-only
#: decision: a session hijacked in a browser should not be able to grant itself
#: the whole disk.
READ_ONLY_PATHS = ("security.file_roots",)


class SettingsPatch(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


class AppSettingsPatch(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


def _clean(values: Dict[str, Any]) -> Dict[str, Any]:
    """Drop untouched secrets and reject read-only paths."""
    out = {}  # type: Dict[str, Any]
    for path, value in values.items():
        if not isinstance(path, str) or not path:
            raise ApiError(400, "bad_key", "Setting keys must be dotted paths.")
        if path in READ_ONLY_PATHS:
            raise ApiError(
                403,
                "read_only_setting",
                "%s can only be changed in the config file on the machine itself." % path,
            )
        if config_module.is_redacted(value):
            # The client is echoing back what we redacted: leave the real one be.
            continue
        out[path] = value
    return out


@router.get("")
async def read_settings(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    return {
        "settings": config.as_dict(redact=True),
        "layers": config.layers(),
        "path": str(config.path),
        "restart_required": list(RESTART_REQUIRED),
        "read_only": list(READ_ONLY_PATHS),
    }


@router.put("")
async def write_settings(
    payload: SettingsPatch,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Apply a flat ``{"server.port": 8099}`` patch and persist it.

    Returns which of the changed paths need a restart, so the UI can show one
    banner instead of guessing.
    """
    values = _clean(payload.values)
    if not values:
        return {"ok": True, "changed": [], "restart_required": []}
    config.set_many(values)
    config.save()
    changed = sorted(values)
    log.info("settings changed by %s: %s", user.get("username"), ", ".join(changed))
    return {
        "ok": True,
        "changed": changed,
        "restart_required": [path for path in changed if path in RESTART_REQUIRED],
        "settings": config.as_dict(redact=True),
    }


@router.get("/apps")
async def list_app_settings(
    config: Any = Depends(get_config),
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Every app that has settings, whether or not it is currently loaded.

    An app that failed to start still shows its configuration -- that is usually
    exactly where the reason is.
    """
    known = []  # type: List[str]
    for record in plugins.list_apps():
        known.append(record["id"])
    for app_id in config.app_ids():
        if app_id not in known:
            known.append(app_id)
    out = []
    for app_id in sorted(known):
        info = plugins.info(app_id) or {}
        out.append(
            {
                "id": app_id,
                "name": info.get("name", app_id),
                "loaded": app_id in plugins,
                "schema": info.get("config_schema") or info.get("schema"),
                "values": config.app(app_id).as_dict(redact=True),
            }
        )
    return {"apps": out}


@router.get("/apps/{app_id}")
async def read_app_settings(
    app_id: str,
    config: Any = Depends(get_config),
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    info = plugins.info(app_id) or {}
    if not info and app_id not in config.app_ids():
        raise ApiError(404, "app_not_found", "No app called %r." % app_id)
    return {
        "id": app_id,
        "name": info.get("name", app_id),
        "schema": info.get("config_schema") or info.get("schema"),
        "values": config.app(app_id).as_dict(redact=True),
    }


@router.put("/apps/{app_id}")
async def write_app_settings(
    app_id: str,
    payload: AppSettingsPatch,
    config: Any = Depends(get_config),
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Change one app's settings and restart it if it is running.

    Restarting is not optional: an app that reads its config at start-up would
    otherwise keep running on the old values, and the settings page would be
    lying about what is in effect.
    """
    if app_id not in plugins and app_id not in config.app_ids():
        raise ApiError(404, "app_not_found", "No app called %r." % app_id)
    values = _clean(payload.values)
    app_config = config.app(app_id)
    if values:
        app_config.set_many(values)
        app_config.save()

    restarted = False
    record = plugins.get(app_id)
    if values and record is not None:
        status = plugins.status_of(record)
        if status.get("state") == "running":
            await plugins.restart(app_id)
            restarted = True

    return {
        "ok": True,
        "changed": sorted(values),
        "restarted": restarted,
        "values": app_config.as_dict(redact=True),
    }


__all__ = ["router"]
