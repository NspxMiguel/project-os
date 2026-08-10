"""Bundled project-os apps.

One sub-package per app id, each with a ``manifest.json`` and an entrypoint
module exposing ``async def setup(ctx) -> AppInstance``. Apps installed by the
user live in ``~/.project_os/apps/<id>/`` and override a bundled app of the same
id; see :mod:`project_os.core.plugins`.
"""

from __future__ import annotations
