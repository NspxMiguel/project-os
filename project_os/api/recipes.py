"""Recipes over HTTP: what to do with a device, and doing the easy parts.

``GET /api/devices/{id}/recipes`` (in :mod:`project_os.api.devices`) answers the
question; this module answers "now do step 2 for me".

:func:`run_step` is deliberately one step at a time rather than "run the whole
recipe". Two reasons, both learned the hard way by other systems: a recipe is
usually part automatic and part not, so "run it all" would be a lie halfway
through; and installing four things because someone clicked one button is the
sort of surprise that makes people stop clicking buttons.

Steps of kind ``manual`` are never run. They are text, and text is for the
person. A ``command:`` in a recipe is shown to be copied -- it came out of a
data file, and data files get edited.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from project_os import auth
from project_os.core import recipes as recipes_core
from project_os.errors import ApiError
from project_os.main import get_config, get_devices, get_plugins

log = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])


class RunStep(BaseModel):
    device_id: str
    step: int
    #: Required if the step installs something that does not fit this board;
    #: passed straight through to the store's own check.
    accept_oversize: bool = False


def installed_ids(plugins: Any) -> List[str]:
    return [item["id"] for item in plugins.list_apps()]


@router.get("")
async def list_recipes(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Every recipe, unrendered. The catalogue of what the box knows how to do."""
    items = recipes_core.all_recipes()
    return {
        "recipes": [
            {
                "id": item["id"],
                "title": item["title"],
                "summary": item.get("summary", ""),
                "icon": item.get("icon", "bulb"),
                "match": item.get("match", {}),
                "steps": len(item["steps"]),
                "automatic": sum(
                    1 for step in item["steps"] if step["kind"] in recipes_core.AUTOMATIC_KINDS
                ),
            }
            for item in items
        ],
        "count": len(items),
    }


@router.post("/run")
async def run_step(
    body: RunStep,
    registry: Any = Depends(get_devices),
    plugins: Any = Depends(get_plugins),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    device = registry.get(body.device_id)
    if device is None:
        raise ApiError(404, "device_not_found", "No device with id %r." % body.device_id)
    return await _run(body, device, plugins, config, None, user)


@router.post("/{recipe_id}/run")
async def run_step_of(
    recipe_id: str,
    body: RunStep,
    registry: Any = Depends(get_devices),
    plugins: Any = Depends(get_plugins),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    device = registry.get(body.device_id)
    if device is None:
        raise ApiError(404, "device_not_found", "No device with id %r." % body.device_id)
    return await _run(body, device, plugins, config, recipe_id, user)


async def _run(
    body: RunStep,
    device: Dict[str, Any],
    plugins: Any,
    config: Any,
    recipe_id: Optional[str] = None,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    recipe = recipes_core.get(recipe_id) if recipe_id else None
    if recipe_id and recipe is None:
        raise ApiError(404, "recipe_not_found", "No recipe called %r." % recipe_id)
    if recipe is None:
        raise ApiError(400, "recipe_required", "Say which recipe the step belongs to.")
    if not recipes_core.matches(recipe, device):
        # Recipes are matched to devices for a reason: "point Zigbee2MQTT at this
        # serial port" makes no sense aimed at a television.
        raise ApiError(
            409,
            "recipe_does_not_apply",
            "%s does not apply to %s." % (recipe["id"], device.get("name") or device["id"]),
        )

    steps = recipe["steps"]
    if body.step < 0 or body.step >= len(steps):
        raise ApiError(404, "no_such_step", "%s has %d steps." % (recipe["id"], len(steps)))

    rendered = recipes_core.render(recipe, device)["steps"][body.step]
    step = steps[body.step]
    kind = step["kind"]

    if kind == recipes_core.STEP_MANUAL:
        raise ApiError(
            400,
            "manual_step",
            "This step is for you to do; the box cannot do it. %s" % rendered["text"],
        )

    if kind == recipes_core.STEP_OPEN:
        # Nothing to run: the browser opens it. Returned anyway so the client has
        # one code path for "press the button on a step".
        return {"ok": True, "kind": kind, "url": rendered["url"], "step": rendered}

    if kind == recipes_core.STEP_CONFIG:
        config.set(step["key"], rendered["value"])
        config.save()
        log.info("recipe %s set %s for %s", recipe["id"], step["key"], device.get("id"))
        return {"ok": True, "kind": kind, "key": step["key"], "value": rendered["value"]}

    # install
    from project_os.api import store

    app_id = step["app"]
    if app_id in installed_ids(plugins):
        return {"ok": True, "kind": kind, "app": app_id, "already_installed": True}
    result = await store.install(
        app_id,
        store.InstallBody(accept_oversize=body.accept_oversize),
        plugins=plugins,
        config=config,
        user=dict(user or {}, recipe=recipe["id"]),
    )
    return {"ok": True, "kind": kind, "app": app_id, "install": result}


__all__ = ["router"]
