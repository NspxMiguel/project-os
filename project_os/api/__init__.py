"""The aggregate API router, mounted at ``/api`` by :func:`project_os.main.create_app`.

Each submodule owns its own prefix (``/auth``, ``/system``, ...) and exposes a
module-level ``router``. Submodules are imported defensively: one broken
endpoint file degrades that feature instead of preventing the box from booting
-- which matters when the only way in is the browser. Set ``PROJECTOS_STRICT_API``
(any value) to turn those failures back into hard errors, as the test suite does.
"""

from __future__ import annotations

import importlib
import logging
import os
import traceback
from typing import Dict, List

from fastapi import APIRouter

log = logging.getLogger(__name__)

#: Import order is the route-matching order.
SUBMODULES = [
    "auth",
    "system",
    "devices",
    "recipes",
    "suggestions",
    "apps",
    "store",
    "installed",
    "packages",
    "updates",
    "helpers",
    "files",
    "tuning",
    "shell",
    "settings",
    "ws",
]

api_router = APIRouter()

_loaded = []  # type: List[str]
_failed = {}  # type: Dict[str, str]


def _load_submodules() -> None:
    strict = bool(os.environ.get("PROJECTOS_STRICT_API"))
    for name in SUBMODULES:
        try:
            module = importlib.import_module(".%s" % name, __name__)
            router = getattr(module, "router", None)
            if router is None:
                raise AttributeError("project_os.api.%s defines no `router`" % name)
            api_router.include_router(router)
        except Exception:
            if strict:
                raise
            _failed[name] = traceback.format_exc()
            log.error("API module %r failed to load:\n%s", name, _failed[name])
        else:
            _loaded.append(name)


def loaded_modules() -> List[str]:
    return list(_loaded)


def failed_modules() -> Dict[str, str]:
    """``{module_name: traceback}`` for everything that did not load."""
    return dict(_failed)


_load_submodules()

__all__ = ["api_router", "SUBMODULES", "loaded_modules", "failed_modules"]
