"""Search and install software over HTTP. The Advanced mode's package manager.

    "o cara foi la, foi pro modo advanced, instalo um firefox, um flatpack
     store, e dps voltau pro modo simple. ai aparece em apps."

Reads (backends, search, job status) need a session. Writes need a session *and*
``security.allow_package_management``, which the installer turns on: installing a
package is arbitrary code running as root, so it is a switch someone flips
knowingly, the same as the shell and the hardware knobs.

Installs are jobs. ``POST /install`` answers immediately with a job id and the
screen follows the log; nothing here blocks for the twenty minutes an apt run can
take on a Pi 3B.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import anyio
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from projectos import auth
from projectos.core import packages
from projectos.errors import ApiError
from projectos.main import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/packages", tags=["packages"])

#: One runner for the process: package managers take a machine-wide lock, so
#: two concurrent installs would fail on each other anyway.
_runner = packages.JobRunner()


class PackageRef(BaseModel):
    source: str = Field(..., min_length=1, description="apt or flatpak")
    package: str = Field(..., min_length=1)


def _require_writes(config: Any) -> None:
    if not config.get("security.allow_package_management", True):
        raise ApiError(
            403,
            "package_management_disabled",
            "Ligue security.allow_package_management em Configurações > Desenvolvedor "
            "para instalar e remover programas por aqui.",
        )


def _translate(exc: packages.PackageError) -> ApiError:
    status = {
        "busy": 409,
        "needs_root": 403,
        "backend_unavailable": 409,
        "unknown_source": 400,
        "bad_package_name": 400,
        "query_too_short": 400,
    }.get(exc.code, 400)
    return ApiError(status, exc.code, exc.message, exc.hint or None)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
@router.get("")
async def overview(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    """What this machine can install, and what is running right now."""
    backends = await anyio.to_thread.run_sync(packages.backends)
    return {
        "backends": backends,
        "enabled": bool(config.get("security.allow_package_management", True)),
        "busy": _runner.busy(),
        "jobs": _runner.list(),
    }


@router.get("/search")
async def search(
    q: str = Query(..., min_length=2, description="What to look for"),
    source: Optional[str] = Query(None, description="Only this backend"),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    sources = [source] if source else None
    try:
        return await anyio.to_thread.run_sync(lambda: packages.search(q, sources))
    except packages.PackageError as exc:
        raise _translate(exc)


@router.get("/jobs")
async def list_jobs(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    return {"jobs": _runner.list(), "busy": _runner.busy()}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    tail: int = Query(80, ge=0, le=packages.LOG_LINES),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    job = _runner.get(job_id)
    if job is None:
        raise ApiError(404, "not_found", "No package job %r." % job_id)
    return {"job": job.as_dict(tail=tail)}


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
@router.post("/install")
async def install(
    body: PackageRef,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require_writes(config)
    try:
        job = _runner.start("install", body.source, body.package)
    except packages.PackageError as exc:
        raise _translate(exc)
    log.info("install requested: %s %s", body.source, body.package)
    return {"job": job.as_dict(tail=0)}


@router.post("/remove")
async def remove(
    body: PackageRef,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require_writes(config)
    try:
        job = _runner.start("remove", body.source, body.package)
    except packages.PackageError as exc:
        raise _translate(exc)
    log.info("removal requested: %s %s", body.source, body.package)
    return {"job": job.as_dict(tail=0)}


__all__ = ["router"]
