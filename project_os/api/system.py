"""System state: health, stats, hardware, logs, services, power.

The dividing line in this file is between *reading* the machine and *changing*
it. Reads need a session. Writes -- rebooting, stopping a systemd unit -- need a
session **and** an explicit opt-in in the config, because project-os is reachable
from any browser on the LAN and "restart the box" should not be one click away
from a login screen by default.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from project_os import __version__, auth
from project_os.core import hardware, sysinfo
from project_os.db import Database
from project_os.errors import ApiError
from project_os.main import get_config, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

#: Units project-os is willing to talk about. An arbitrary unit name from the
#: browser is not accepted -- that would be a remote root shell with extra steps.
MANAGED_UNIT_PREFIXES = ("project_os", "home-assistant", "mosquitto", "zigbee2mqtt",
                         "esphome", "nodered", "zwave-js-ui", "argos", "pihole-FTL",
                         "syncthing", "uptime-kuma", "scrypted")

SYSTEMCTL_TIMEOUT = 15.0


class ServiceAction(BaseModel):
    action: str = Field(..., pattern="^(start|stop|restart)$")


class PowerAction(BaseModel):
    action: str = Field(..., pattern="^(reboot|shutdown)$")
    confirm: bool = False


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
@router.get("/health")
async def health(request: Request, db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Public liveness probe. Deliberately says almost nothing."""
    return {
        "status": "ok",
        "version": __version__,
        "setup_required": auth.setup_required(db),
        "started_at": getattr(request.app.state, "started_at", None),
    }


@router.get("/stats")
async def stats(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    """The same payload the ``system.stats`` websocket topic pushes every 5s."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sysinfo.stats)


@router.get("/info")
async def info(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sysinfo.info)


@router.get("/hardware")
async def board(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    """What board this is and what it can carry.

    ``tier`` is what the store reads to decide whether to offer an app; the
    frontend shows ``reason`` when ``supported`` is false. See
    :mod:`project_os.core.hardware` for why the gate is measured RAM rather than
    a list of model names.
    """
    detected = hardware.detect()
    payload = detected.as_dict()
    payload["minimum_ram_mb"] = hardware.MINIMUM_RAM_MB
    payload["tiers"] = [
        {"floor_mb": floor, "name": name, "note": note} for floor, name, note in hardware.TIERS
    ]
    return payload


@router.get("/logs")
async def logs(
    limit: int = Query(200, ge=1, le=2000),
    level: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    db: Database = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return {"lines": db.recent_log(limit=limit, level=level, source=source)}


@router.delete("/logs")
async def clear_logs(
    db: Database = Depends(get_db), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    db.execute("DELETE FROM log")
    return {"ok": True}


@router.get("/services")
async def services(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    """The systemd units project-os knows about.

    Returns an empty list rather than an error where there is no systemd, so the
    Advanced view degrades to "nothing to manage here" on a developer's laptop.
    """
    if not shutil.which("systemctl"):
        return {"available": False, "services": [], "can_control": False}
    units = await _list_units()
    return {
        "available": True,
        "services": units,
        "can_control": bool(config.get("security.allow_service_control", False)),
    }


async def _list_units() -> List[Dict[str, Any]]:
    output = await _run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain",
         "--no-legend"]
    )
    units = []  # type: List[Dict[str, Any]]
    for line in (output or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        name = parts[0]
        if not name.endswith(".service"):
            continue
        stem = name[: -len(".service")]
        if not any(stem.startswith(prefix) for prefix in MANAGED_UNIT_PREFIXES):
            continue
        units.append(
            {
                "unit": name,
                "name": stem,
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return sorted(units, key=lambda item: item["name"])


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
@router.post("/services/{unit}")
async def control_service(
    unit: str,
    payload: ServiceAction,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return await _control(unit, payload.action, config, user)


@router.post("/services/{unit}/{action}")
async def control_service_by_path(
    unit: str,
    action: str,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """The same thing with the verb in the URL, for links and shell one-liners.

    Validated here rather than by a model, since a path segment cannot carry a
    request body to validate.
    """
    if action not in ("start", "stop", "restart"):
        raise ApiError(400, "unknown_action", "No such service action %r." % action)
    return await _control(unit, action, config, user)


async def _control(
    unit: str, action: str, config: Any, user: Dict[str, Any]
) -> Dict[str, Any]:
    if not config.get("security.allow_service_control", False):
        raise ApiError(
            403,
            "service_control_disabled",
            "Turn on security.allow_service_control to start and stop services.",
        )
    stem = unit[: -len(".service")] if unit.endswith(".service") else unit
    if not any(stem.startswith(prefix) for prefix in MANAGED_UNIT_PREFIXES):
        raise ApiError(
            403, "unit_not_managed", "project-os does not manage the unit %r." % unit
        )
    if not shutil.which("systemctl"):
        raise ApiError(503, "no_systemd", "There is no systemctl on this machine.")
    await _run(["systemctl", action, "%s.service" % stem], check=True)
    log.info("%s %s.service (by %s)", action, stem, user.get("username"))
    return {"ok": True, "unit": "%s.service" % stem, "action": action}


@router.post("/power")
async def power(
    payload: PowerAction,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Reboot or shut down the board.

    Guarded twice: the same config flag as service control, plus an explicit
    ``confirm``. Pulling the plug on a running Pi is one of the few things here
    that can corrupt an SD card, and the second guard means a mis-routed fetch
    cannot do it.
    """
    if not config.get("security.allow_service_control", False):
        raise ApiError(
            403,
            "power_control_disabled",
            "Turn on security.allow_service_control to reboot from the browser.",
        )
    if not payload.confirm:
        raise ApiError(400, "confirm_required", "Send confirm=true to %s." % payload.action)
    command = ["systemctl", "reboot" if payload.action == "reboot" else "poweroff"]
    if not shutil.which("systemctl"):
        raise ApiError(503, "no_systemd", "There is no systemctl on this machine.")
    log.warning("%s requested by %s", payload.action, user.get("username"))
    # Fire and forget: the box goes away before the command returns, so awaiting
    # it would guarantee a timeout error on an operation that actually worked.
    asyncio.ensure_future(_run(command))
    return {"ok": True, "action": payload.action}


async def _run(command: List[str], check: bool = False) -> Optional[str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), SYSTEMCTL_TIMEOUT)
    except asyncio.TimeoutError:
        raise ApiError(504, "command_timeout", "%s took too long." % command[0])
    except (OSError, ValueError) as exc:
        raise ApiError(503, "command_failed", "Could not run %s: %s" % (command[0], exc))
    if check and process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip()
        raise ApiError(500, "command_failed", message or "%s failed." % " ".join(command))
    return (stdout or b"").decode("utf-8", "replace")


__all__ = ["router"]
