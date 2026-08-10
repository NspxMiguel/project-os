"""The device list, and the scan that fills it."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from project_os import auth
from project_os.errors import ApiError
from project_os.main import get_devices, get_plugins

log = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    pinned: Optional[bool] = None
    ignored: Optional[bool] = None


@router.get("")
async def list_devices(
    include_ignored: bool = Query(False),
    registry: Any = Depends(get_devices),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return {
        "devices": registry.devices(include_ignored=include_ignored),
        "summary": registry.summary(),
    }


@router.post("/scan")
async def scan(
    registry: Any = Depends(get_devices),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Force a scan now.

    Takes several seconds -- mDNS browsing and UDP listening both need time to
    hear from things. Progress arrives on the websocket as ``device.found``
    events, so the UI can fill in as they come rather than waiting on this.
    """
    devices = await registry.scan()
    return {"devices": devices, "summary": registry.summary()}


@router.get("/{device_id}")
async def get_device(
    device_id: str,
    registry: Any = Depends(get_devices),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    device = registry.get(device_id)
    if device is None:
        raise ApiError(404, "device_not_found", "Não existe aparelho com id %r." % device_id)
    # to_dict, not the object: this route says it returns a mapping, and FastAPI
    # believes it. Returning the dataclass raised ResponseValidationError, so
    # every device's own page answered 500 -- the whole screen was dead.
    return device.to_dict()


@router.get("/{device_id}/recipes")
async def device_recipes(
    device_id: str,
    registry: Any = Depends(get_devices),
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """What you can do with this device, most specific first.

    Every step says whether the box can do it for you, so the screen can lead
    with "4 of 5 steps are one button" instead of a wall of instructions.
    """
    from project_os.core import recipes as recipes_core

    device = registry.get(device_id)
    if device is None:
        raise ApiError(404, "device_not_found", "Não existe aparelho com id %r." % device_id)
    installed = [item["id"] for item in plugins.list_apps()]
    # recipes.for_device reads the device like a mapping (device.get(...)), so it
    # gets the mapping. Handing it the dataclass raised AttributeError and the
    # "what you can do with this device" card only ever showed an error.
    payload = device.to_dict()
    found = recipes_core.for_device(payload, installed=installed)
    return {
        "device": payload,
        "recipes": found,
        "count": len(found),
        "automatic": sum(item["automatic"] for item in found),
    }


@router.patch("/{device_id}")
async def update_device(
    device_id: str,
    payload: DeviceUpdate,
    registry: Any = Depends(get_devices),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    if registry.get(device_id) is None:
        raise ApiError(404, "device_not_found", "Não existe aparelho com id %r." % device_id)
    if payload.name is not None:
        registry.rename(device_id, payload.name.strip() or None)
    if payload.pinned is not None:
        registry.set_flag(device_id, "pinned", payload.pinned)
    if payload.ignored is not None:
        registry.set_flag(device_id, "ignored", payload.ignored)
    return registry.get(device_id).to_dict()


@router.delete("/{device_id}")
async def forget_device(
    device_id: str,
    registry: Any = Depends(get_devices),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Forget a device.

    Not the same as ignoring it: a forgotten device comes straight back on the
    next scan, minus its custom name. Use ``ignored`` to make it stay away.
    """
    if not registry.forget(device_id):
        raise ApiError(404, "device_not_found", "Não existe aparelho com id %r." % device_id)
    return {"ok": True, "id": device_id}


__all__ = ["router"]
