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
import os
import shutil
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from project_os import __version__, auth, paths
from project_os.core import clock, hardware, slots, syspass, sysinfo, updates
from project_os.db import Database
from project_os.errors import ApiError
from project_os.main import get_config, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

#: Units project-os is willing to talk about. An arbitrary unit name from the
#: browser is not accepted -- that would be a remote root shell with extra steps.
#
# "project-os" with a hyphen: the units are ``project-os.service`` and
# ``project-os-firstboot.service``. The pre-rename spelling survived here and
# matched nothing, so the Services screen on a real box listed zero units --
# including project-os's own.
MANAGED_UNIT_PREFIXES = ("project-os", "home-assistant", "mosquitto", "zigbee2mqtt",
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
    """Public liveness probe. Deliberately says almost nothing.

    "Almost": também diz se o esquema de dois sistemas está de pé.
    Isso é público de propósito, e é o único jeito de conferir um cartão recém
    gravado -- antes de existir conta não existe sessão, e a pergunta "o
    reparticionamento aconteceu?" é justamente a que ninguém consegue responder
    nessa hora sem tirar o cartão do Pi. Não conta nada que um curioso na rede já
    não veja: versão e "ainda não tem dono" já saíam aqui.
    """
    return {
        "status": "ok",
        "version": __version__,
        "setup_required": auth.setup_required(db),
        "started_at": clock.started_at(request.app.state),
        "recovery": _estado_dos_slots(request.app.state),
    }


def _slot_atual(state: Any) -> Optional[str]:
    """Em qual slot este sistema está rodando -- descoberto uma vez só.

    Não muda sem reiniciar, e descobrir custa um ``findmnt``. O health é chamado
    a cada segundo e meio durante uma atualização: sem esta memória seria um
    processo por batida. Guardado no ``app.state`` e não num global do módulo
    para não atravessar de um app para outro (nos testes, de um teste para o
    seguinte).
    """
    lembrado = getattr(state, "slot_deste_boot", ...)
    if lembrado is ...:
        lembrado = slots.current_slot()
        state.slot_deste_boot = lembrado
    return lembrado


def _estado_dos_slots(state: Any) -> Dict[str, Any]:
    """Resumo barato: o slot vem da memória, o resto é um arquivo. Nunca levanta."""
    try:
        atual = _slot_atual(state)
        estado = slots.read_state() if atual else {}
        return {
            "slots": bool(atual) and os.path.isfile(slots.state_path()),
            "slot": atual or "",
            "good": str(estado.get("good") or ""),
            "tries": int(estado.get("tries") or 0),
            "data_partition": os.path.ismount(str(paths.home())),
        }
    except Exception:  # pragma: no cover - um health nunca pode ser o que quebra
        return {"slots": False, "slot": "", "good": "", "tries": 0, "data_partition": False}


@router.get("/stats")
async def stats(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    """The same payload the ``system.stats`` websocket topic pushes every 5s."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sysinfo.stats)


@router.get("/processes")
async def processes(
    limit: int = Query(20, ge=1, le=100),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """The heaviest processes, memory first.

    ``sysinfo.top_processes`` existed from the start and nothing ever served it,
    so the System page's "Top processes" card asked for a route that answered
    404 and then quietly said nothing. On a Pi that card is the whole reason to
    open the page: it is where "why is this thing slow" gets an answer.
    """
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, sysinfo.top_processes, limit)
    return {"processes": rows, "psutil": sysinfo.have_psutil()}


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
        raise ApiError(400, "unknown_action", "Não existe a ação de serviço %r." % action)
    return await _control(unit, action, config, user)


async def _control(
    unit: str, action: str, config: Any, user: Dict[str, Any]
) -> Dict[str, Any]:
    if not config.get("security.allow_service_control", False):
        raise ApiError(
            403,
            "service_control_disabled",
            "Ligue o security.allow_service_control para iniciar e parar serviços.",
        )
    stem = unit[: -len(".service")] if unit.endswith(".service") else unit
    if not any(stem.startswith(prefix) for prefix in MANAGED_UNIT_PREFIXES):
        raise ApiError(
            403, "unit_not_managed", "O project-os não cuida da unidade %r." % unit
        )
    if not shutil.which("systemctl"):
        raise ApiError(503, "no_systemd", "Não existe systemctl nesta máquina.")
    # Com sudo quando este processo não é root -- ver updates.systemctl_argv.
    # Sem isso, todo botão desta tela falha num Pi de verdade: o serviço roda
    # como usuário project-os e o logind recusa um systemctl restart de quem não
    # tem sessão, com "Interactive authentication required".
    await _run(updates.systemctl_argv() + [action, "%s.service" % stem], check=True)
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
            "Ligue o security.allow_service_control para reiniciar pelo navegador.",
        )
    if not payload.confirm:
        raise ApiError(400, "confirm_required", "Send confirm=true to %s." % payload.action)
    if not shutil.which("systemctl"):
        raise ApiError(503, "no_systemd", "Não existe systemctl nesta máquina.")
    command = updates.systemctl_argv() + [
        "reboot" if payload.action == "reboot" else "poweroff"
    ]
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
        raise ApiError(504, "command_timeout", "%s demorou demais." % command[0])
    except (OSError, ValueError) as exc:
        raise ApiError(503, "command_failed", "Não consegui rodar %s: %s" % (command[0], exc))
    if check and process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip()
        raise ApiError(500, "command_failed", message or "%s failed." % " ".join(command))
    return (stdout or b"").decode("utf-8", "replace")


__all__ = ["router"]


# --------------------------------------------------------------------------- ssh


class SystemPassword(BaseModel):
    password: str = Field(..., min_length=1)


@router.get("/password")
async def password_state(
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Whether the Linux account's password can be set from here."""
    return syspass.available()


@router.post("/password")
async def set_system_password(
    body: SystemPassword,
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Set the password of the Linux account used for SSH.

    A public image cannot ship a real password, so it ships none at all and the
    account is locked. This is how it gets one -- from the same screen that
    already asks you to invent an administrator password.
    """
    result = syspass.set_password(body.password)
    if not result["ok"]:
        status = {
            "unavailable": 501,
            "needs_root": 403,
            "empty": 400,
            "bad_password": 400,
        }.get(result["code"], 500)
        raise ApiError(status, result["code"], result["message"], result.get("hint"))
    log.info("system password changed by %s", user.get("username"))
    return {"ok": True, "user": result.get("user", "")}
