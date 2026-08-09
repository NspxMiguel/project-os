"""What is already on the machine, whoever put it there.

    "adiciona tbm apps, detectados no proprio sistema, sla, o cara foi la, foi
    pro modo advanced, instalo um firefox, um flatpack store, e dps voltau pro
    modo simple. ai aparece em apps."

Read-only, on purpose. This endpoint lists; removing a package someone installed
by hand belongs at a terminal, where the consequences are visible.

The scan shells out to half a dozen tools, so it runs in a thread and the result
is cached for a minute. Opening the apps screen twice should not run ``apt-mark``
twice.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import anyio
from fastapi import APIRouter, Depends, Query

from projectos import auth
from projectos.core import installed as installed_core

log = logging.getLogger(__name__)

router = APIRouter(prefix="/installed", tags=["installed"])

CACHE_SECONDS = 60.0
_cache = None  # type: Optional[Dict[str, Any]]
_cached_at = 0.0


@router.get("")
async def list_installed(
    refresh: bool = Query(False, description="Skip the cache and look again"),
    source: Optional[str] = Query(None, description="Only this source"),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    global _cache, _cached_at

    if source:
        result = await anyio.to_thread.run_sync(lambda: installed_core.scan([source]))
        return _shape(result, cached=False)

    age = time.monotonic() - _cached_at
    if _cache is None or refresh or age > CACHE_SECONDS:
        _cache = await anyio.to_thread.run_sync(installed_core.scan)
        _cached_at = time.monotonic()
        age = 0.0
    return _shape(_cache, cached=age > 0, age_seconds=round(age, 1))


def _shape(result: Dict[str, Any], cached: bool, age_seconds: float = 0.0) -> Dict[str, Any]:
    items = result["items"]
    return {
        "items": items,
        "count": len(items),
        "counts": result["counts"],
        "sources": [name for name, _ in installed_core.SOURCES],
        #: Which sources produced nothing because the tool is not on this
        #: machine. Worth showing: "0 flatpaks" and "no flatpak here" look the
        #: same in a list and mean different things.
        "errors": result["errors"],
        "catalog_matches": result["catalog_matches"],
        "cached": cached,
        "age_seconds": age_seconds,
    }


__all__ = ["router"]
