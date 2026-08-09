"""Hardware knobs over HTTP: fan, clock, LEDs, HDMI, Wi-Fi, GPU split.

Reads are open to any session. Every write goes through :func:`_require`, which
checks ``security.allow_hardware_control`` first and root second, and says which
one is missing -- "you are not root" and "you did not turn this on" need
different fixes, and guessing between them is annoying.

Errors from :mod:`projectos.core.tuning` are translated here, once, in
:func:`_apply`: ``LookupError`` means the board does not have that knob (404),
``ValueError`` means the value is out of range (400), ``OSError`` means the write
itself failed (500). The core module stays free of HTTP.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from projectos import auth
from projectos.core import tuning
from projectos.errors import ApiError
from projectos.main import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tuning", tags=["tuning"])


class FanLevel(BaseModel):
    level: int = Field(..., ge=0, le=255)
    device: Optional[str] = None


class FanStep(BaseModel):
    step: int = Field(..., ge=0, le=3)
    celsius: int = Field(..., ge=30, le=85)
    speed: int = Field(255, ge=0, le=255)


class FanCurve(BaseModel):
    steps: List[FanStep]


class Governor(BaseModel):
    governor: str = Field(..., min_length=1)


class MaxClock(BaseModel):
    #: ``None`` puts the board back to its stock speed.
    mhz: Optional[int] = None


class LedState(BaseModel):
    on: bool
    trigger: Optional[str] = None


class Toggle(BaseModel):
    on: bool
    interface: Optional[str] = None


class GpuSplit(BaseModel):
    mb: int = Field(..., ge=16, le=512)


def _require(config: Any) -> None:
    if not config.get("security.allow_hardware_control", False):
        raise ApiError(
            403,
            "hardware_control_disabled",
            "Ligue security.allow_hardware_control em Configurações > Desenvolvedor "
            "para mexer em ventoinha, clock e LEDs.",
        )
    if not tuning.is_root():
        raise ApiError(
            403,
            "not_root",
            "Estas mudanças precisam de root. O serviço systemd do ProjectOS roda "
            "como root; um processo aberto na mão pelo terminal, não.",
        )


def _apply(action: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return action()
    except LookupError as exc:
        raise ApiError(404, "not_available", str(exc))
    except ValueError as exc:
        raise ApiError(400, "out_of_range", str(exc))
    except PermissionError as exc:
        raise ApiError(403, "permission_denied", str(exc))
    except OSError as exc:
        raise ApiError(500, "write_failed", str(exc))


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
@router.get("")
async def snapshot(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    payload = tuning.snapshot()
    payload["enabled"] = bool(config.get("security.allow_hardware_control", False))
    if not payload["enabled"]:
        payload["writable"] = False
        payload["reason"] = (
            "Controle de hardware está desligado. Configurações > Desenvolvedor liga."
        )
    return payload


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
@router.post("/fan/level")
async def fan_level(
    body: FanLevel,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_fan_level(body.level, body.device))


@router.post("/fan/curve")
async def fan_curve(
    body: FanCurve,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """The one that actually makes a Pi 5 quiet. Applies on the next boot."""
    _require(config)
    steps = [step.dict() for step in body.steps]
    return _apply(lambda: tuning.set_fan_thresholds(steps))


@router.post("/clock/governor")
async def governor(
    body: Governor,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_governor(body.governor))


@router.post("/clock/max")
async def max_clock(
    body: MaxClock,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_max_clock(body.mhz))


@router.post("/leds/{name}")
async def led(
    name: str,
    body: LedState,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_led(name, body.on, body.trigger))


@router.post("/hdmi")
async def hdmi(
    body: Toggle,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_hdmi(body.on))


@router.post("/wifi/power-save")
async def wifi_power_save(
    body: Toggle,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_wifi_power_save(body.on, body.interface))


@router.post("/gpu-memory")
async def gpu_memory(
    body: GpuSplit,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require(config)
    return _apply(lambda: tuning.set_gpu_memory(body.mb))


__all__ = ["router"]
