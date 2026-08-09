"""The suggestion cards, and doing what they suggest."""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query, Request

from projectos import auth
from projectos.errors import ApiError
from projectos.main import get_suggestions

log = logging.getLogger(__name__)

router = APIRouter(prefix="/suggestions", tags=["suggestions"])


@router.get("")
async def list_suggestions(
    include_dismissed: bool = Query(False),
    engine: Any = Depends(get_suggestions),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    items = engine.evaluate(include_dismissed=include_dismissed)
    return {"suggestions": items, "count": len(items)}


@router.post("/{suggestion_id}/dismiss")
async def dismiss(
    suggestion_id: str,
    engine: Any = Depends(get_suggestions),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Dismiss for good.

    Dismissal is not "hide until next scan" -- a card the user has said no to
    must not come back, or the whole feature becomes something to ignore.
    """
    return engine.dismiss(suggestion_id)


@router.post("/{suggestion_id}/restore")
async def restore(
    suggestion_id: str,
    engine: Any = Depends(get_suggestions),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return engine.restore(suggestion_id)


@router.post("/{suggestion_id}/apply")
async def apply(
    suggestion_id: str,
    request: Request,
    engine: Any = Depends(get_suggestions),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Carry out a card's action, when it is one that can be done in one step.

    Only ``enable_app`` is executed here. Cards whose action is ``configure`` or
    ``open`` are navigation: they need choices this endpoint has no business
    guessing, so it hands the destination back and the UI takes the user there.
    """
    suggestion = engine.get(suggestion_id)
    if suggestion is None:
        raise ApiError(404, "suggestion_not_found", "No suggestion %r." % suggestion_id)
    action = suggestion.get("action") or {}
    kind = action.get("type")

    if kind == "enable_app":
        plugins = getattr(request.app.state, "plugins", None)
        if plugins is None:
            raise ApiError(503, "not_ready", "The app manager is not available.")
        app_id = action.get("app")
        if not app_id or app_id not in plugins:
            raise ApiError(404, "app_not_found", "No app called %r." % app_id)
        result = await plugins.enable(app_id)
        engine.mark_applied(suggestion_id)
        return {"ok": True, "applied": True, "app": result}

    engine.mark_applied(suggestion_id)
    return {"ok": True, "applied": False, "navigate": action}


__all__ = ["router"]
