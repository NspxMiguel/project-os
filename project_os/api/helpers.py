"""Ajudantes over HTTP: pairing, the registry, the job queue, and the agent link.

Two audiences share this router, and they authenticate differently:

* **The browser** -- the logged-in user, minting pairing codes and looking at
  what is connected. Normal session auth.
* **The agent** -- a PC or an ESP32 out on the network, holding a helper token.
  It never has a session and never should: a helper is not a person, and a
  stolen agent token must not be able to read your files or reboot the box.

That is why the agent's endpoints live under ``/helpers/agent/*`` and check the
token themselves instead of depending on :func:`auth.require_auth`.

The link is a websocket the *agent opens to the Pi*. The Pi never dials out to a
helper: laptops move between networks, sleep, and sit behind routers nobody is
going to configure. Outbound-only means pairing once is enough forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.websockets import WebSocketState

from project_os import auth
from project_os.core import helpers as core
from project_os.errors import ApiError
from project_os.main import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/helpers", tags=["helpers"])

#: How long the agent socket waits for a job before saying hello again. Also the
#: heartbeat: a helper that has not spoken in ``ONLINE_GRACE_SECONDS`` is offline,
#: so this has to be comfortably shorter than that.
POLL_SECONDS = 15.0

CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_READY = 4503


# --------------------------------------------------------------------------- bodies


class NewCode(BaseModel):
    #: Restrict the code to one kind of machine, when you know what you are
    #: pairing. Left empty, any agent may use it.
    kind: Optional[str] = None
    ttl: float = core.CODE_TTL_SECONDS


class Pairing(BaseModel):
    code: str
    name: str = ""
    kind: str = core.KIND_OTHER
    platform: str = ""
    capabilities: List[str] = []
    facts: Dict[str, Any] = {}


class Rename(BaseModel):
    name: str


class Enable(BaseModel):
    enabled: bool


class NewJob(BaseModel):
    kind: str
    payload: Dict[str, Any] = {}
    needs: List[str] = []


class JobResult(BaseModel):
    result: Any = None
    error: str = ""


# --------------------------------------------------------------------------- browser side


@router.get("")
async def list_helpers(
    kind: Optional[str] = None,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    core.requeue_stale(db)
    items = core.helpers(db, kind=kind)
    return {
        "helpers": items,
        "count": len(items),
        "stats": core.stats(db),
        "kinds": list(core.KINDS),
        "capability_labels": core.CAPABILITY_LABELS,
    }


@router.post("/codes")
async def create_code(
    body: NewCode,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Mint a pairing code to type into the agent."""
    if body.ttl <= 0 or body.ttl > 86400:
        raise ApiError(400, "bad_ttl", "A pairing code lives between a second and a day.")
    try:
        return core.new_code(db, kind=body.kind, ttl=body.ttl)
    except RuntimeError as exc:  # pragma: no cover - needs 20 collisions in a row
        raise ApiError(503, "no_code_available", str(exc))


@router.get("/codes")
async def list_codes(
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    items = core.open_codes(db)
    return {"codes": items, "count": len(items)}


@router.delete("/codes/{code}")
async def delete_code(
    code: str,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    return {"ok": core.revoke_code(db, code)}


@router.get("/jobs")
async def list_jobs(
    state: Optional[str] = None,
    limit: int = 100,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    if state and state not in core.JOB_STATES:
        raise ApiError(400, "bad_state", "Unknown job state %r." % state)
    core.requeue_stale(db)
    items = core.jobs(db, state=state, limit=max(1, min(int(limit), 500)))
    return {"jobs": items, "count": len(items), "states": list(core.JOB_STATES)}


@router.post("/jobs")
async def create_job(
    body: NewJob,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Queue a task. It waits until a helper that can run it shows up.

    Queueing with nobody around is allowed on purpose -- the answer flags it so
    the screen can say "nothing here can do this yet" instead of pretending.
    """
    if not body.kind.strip():
        raise ApiError(400, "kind_required", "A job needs a kind.")
    unknown = [n for n in body.needs if n not in core.CAPABILITIES]
    if unknown:
        raise ApiError(400, "unknown_capability", "Nothing offers: %s." % ", ".join(unknown))
    job = core.submit(db, body.kind, payload=body.payload, needs=body.needs)
    return {"job": job, "will_run": core.can_run(db, body.needs)}


@router.delete("/jobs/{job_id}")
async def cancel_job(
    job_id: str,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    job = core.cancel(db, job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "No job with id %r." % job_id)
    return {"job": job}


@router.get("/{helper_id}")
async def show_helper(
    helper_id: str,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    helper = _need(db, helper_id)
    return {"helper": helper, "jobs": core.jobs_of(db, helper_id)}


@router.patch("/{helper_id}")
async def update_helper(
    helper_id: str,
    body: Rename,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _need(db, helper_id)
    return {"helper": core.rename(db, helper_id, body.name)}


@router.post("/{helper_id}/enabled")
async def enable_helper(
    helper_id: str,
    body: Enable,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _need(db, helper_id)
    return {"helper": core.set_enabled(db, helper_id, body.enabled)}


@router.delete("/{helper_id}")
async def delete_helper(
    helper_id: str,
    db: Any = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _need(db, helper_id)
    return {"ok": core.forget(db, helper_id)}


def _need(db: Any, helper_id: str) -> Dict[str, Any]:
    helper = core.get(db, helper_id)
    if helper is None:
        raise ApiError(404, "helper_not_found", "No helper with id %r." % helper_id)
    return helper


# --------------------------------------------------------------------------- agent side


@router.post("/pair")
async def pair(body: Pairing, db: Any = Depends(get_db)) -> Dict[str, Any]:
    """Trade a pairing code for an identity. The only agent call without a token.

    Unauthenticated by necessity -- the agent has nothing yet but the six digits
    the user just read off the screen. That is what makes the code short-lived
    and single-use.
    """
    try:
        result = core.pair(
            db,
            code=body.code,
            name=body.name,
            kind=body.kind,
            platform=body.platform,
            capabilities=body.capabilities,
            facts=body.facts,
        )
    except core.PairingError as exc:
        raise ApiError(400, exc.reason, exc.message)
    return {
        "helper": result["helper"],
        "token": result["token"],
        "note": "guarde esse token, ele nao aparece de novo",
        "poll_seconds": POLL_SECONDS,
    }


def _agent(db: Any, token: Optional[str]) -> Dict[str, Any]:
    helper = core.authenticate(db, token or "")
    if helper is None:
        raise ApiError(401, "bad_helper_token", "This token belongs to no active helper.")
    return helper


@router.post("/agent/heartbeat")
async def heartbeat(
    body: Dict[str, Any],
    db: Any = Depends(get_db),
) -> Dict[str, Any]:
    """The HTTP alternative to the websocket, for agents too small to hold one.

    An ESP32 that wakes every minute, reports a temperature and sleeps again has
    no business keeping a socket open.
    """
    helper = _agent(db, body.get("token"))
    updated = core.touch(
        db,
        helper["id"],
        facts=body.get("facts"),
        capabilities=body.get("capabilities"),
    )
    job = core.take(db, updated or helper)
    return {"helper": updated, "job": job, "poll_seconds": POLL_SECONDS}


@router.post("/agent/jobs/{job_id}/done")
async def job_done(
    job_id: str,
    body: JobResult,
    token: str = "",
    db: Any = Depends(get_db),
) -> Dict[str, Any]:
    helper = _agent(db, token)
    job = core.get_job(db, job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "No job with id %r." % job_id)
    if job.get("helper_id") not in (None, helper["id"]):
        raise ApiError(409, "not_your_job", "That job belongs to another helper.")
    if body.error:
        return {"job": core.fail(db, job_id, body.error)}
    return {"job": core.finish(db, job_id, body.result)}


# --------------------------------------------------------------------------- the link


@router.websocket("/agent/link")
async def agent_link(websocket: WebSocket) -> None:
    """The agent's long-lived connection: heartbeat in, jobs out.

    The agent opens this and leaves it open. Every message it sends is also a
    heartbeat, which is why a helper going offline needs no detection logic --
    it simply stops arriving.
    """
    db = getattr(websocket.app.state, "db", None)
    if db is None:  # pragma: no cover - only during a broken startup
        await websocket.accept()
        await websocket.close(code=CLOSE_NOT_READY)
        return

    token = websocket.query_params.get("token") or ""
    helper = core.authenticate(db, token)
    if helper is None:
        # Accepted first, then closed: a refused handshake reaches the client as
        # a bare 1006 with no reason, so the code would never be seen.
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return

    await websocket.accept()
    helper_id = helper["id"]
    core.touch(db, helper_id)
    bus = getattr(websocket.app.state, "bus", None)
    if bus is not None:
        bus.publish_nowait("helpers.online", {"helper": core.get(db, helper_id)})
    log.info("helper %s connected", helper.get("name") or helper_id)

    reader = asyncio.ensure_future(_read_agent(websocket, db, helper_id))
    writer = asyncio.ensure_future(_feed_agent(websocket, db, helper_id))
    try:
        await websocket.send_json(
            {"type": "hello", "helper": core.get(db, helper_id), "poll_seconds": POLL_SECONDS}
        )
        done, _pending = await asyncio.wait(
            [reader, writer], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    except (ConnectionError, RuntimeError) as exc:
        log.debug("helper link ended: %s", exc)
    finally:
        for task in (reader, writer):
            task.cancel()
        for task in (reader, writer):
            # CancelledError is a BaseException since 3.8, so it has to be named
            # here: awaiting the task we just cancelled is the normal path, not
            # an error, and letting it escape kills the tidy-up below.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        # Whatever it was running is not going to finish now.
        core.release_jobs(db, helper_id)
        if bus is not None:
            bus.publish_nowait("helpers.offline", {"helper_id": helper_id})
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


async def _read_agent(websocket: WebSocket, db: Any, helper_id: str) -> None:
    """Results, heartbeats and capability updates coming from the helper."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        data = _payload(message)
        if data is None:
            continue
        core.touch(db, helper_id, facts=data.get("facts"), capabilities=data.get("capabilities"))
        kind = data.get("type")
        if kind == "done":
            core.finish(db, str(data.get("job") or ""), data.get("result"))
        elif kind == "failed":
            core.fail(db, str(data.get("job") or ""), str(data.get("error") or ""))
        elif kind == "event":
            # A sensor reading, a button press. Straight onto the bus so apps and
            # the dashboard see it without helpers knowing either exists.
            bus = getattr(websocket.app.state, "bus", None)
            if bus is not None:
                bus.publish_nowait(
                    "helpers.event",
                    {"helper_id": helper_id, "name": data.get("name"), "data": data.get("data")},
                )


def _payload(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import json

    text = message.get("text")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        log.debug("a helper sent something that was not JSON")
        return None
    return data if isinstance(data, dict) else None


async def _feed_agent(websocket: WebSocket, db: Any, helper_id: str) -> None:
    """Hand the helper work as it appears.

    Polling rather than waking on a bus event, and deliberately: the queue is the
    single source of truth about who has what, and a poll that finds nothing
    costs one indexed SELECT every fifteen seconds.
    """
    while True:
        helper = core.get(db, helper_id)
        if helper is None or not helper.get("enabled"):
            await websocket.close(code=CLOSE_UNAUTHENTICATED)
            return
        job = core.take(db, helper)
        if job is not None:
            await websocket.send_json({"type": "job", "job": job})
            continue
        await asyncio.sleep(POLL_SECONDS)


__all__ = ["router"]
