"""The live event stream at ``/api/ws``.

One socket per browser tab, carrying every topic on the bus. The frontend
(``web/lib/ws.js``) filters client-side, which is the right trade here: the whole
event volume is a stats payload every 5 seconds plus whatever the apps emit, and
a subscription protocol would be more code on both ends for no gain.

Two things this file is careful about:

* **A slow client must not stall the bus.** Each connection gets its own bounded
  :class:`~projectos.events.Subscription`, which drops oldest-first when it
  fills. A backgrounded tab loses old stats frames; it never holds up a
  publisher.
* **The socket is closed, not errored, when auth fails.** A WebSocket has no
  status code the browser can read, so the close code carries the reason:
  ``4401`` unauthenticated, ``4428`` setup not finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from projectos import auth

log = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])

#: How long to wait for the next event before sending a keepalive frame. Must be
#: comfortably under the 75 s silence timeout in ``web/lib/ws.js``.
IDLE_PING_SECONDS = 20.0

CLOSE_UNAUTHENTICATED = 4401
CLOSE_SETUP_REQUIRED = 4428
CLOSE_NOT_READY = 4503


def _authenticate(websocket: WebSocket) -> Optional[int]:
    """Return a close code, or ``None`` when the connection may proceed.

    ``auth.current_user`` takes any Starlette connection, not only a Request, so
    the cookie the browser already sends is enough -- WebSocket clients cannot
    set an Authorization header. ``?token=`` exists for everything that is not a
    browser.
    """
    if not auth.auth_enabled(websocket):
        return None
    db = getattr(websocket.app.state, "db", None)
    if db is None:
        return CLOSE_NOT_READY
    if auth.setup_required(db):
        return CLOSE_SETUP_REQUIRED
    if auth.current_user(websocket) is not None:
        return None
    token = websocket.query_params.get("token")
    if token and auth.session_user(db, token) is not None:
        return None
    return CLOSE_UNAUTHENTICATED


async def _drain_client(websocket: WebSocket) -> None:
    """Read and discard whatever the client sends.

    Its heartbeat is the only thing that arrives and needs no reply -- but the
    socket must still be read, because an unread receive buffer is how a
    WebSocket silently wedges.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))


async def _pump(websocket: WebSocket, subscription: Any) -> None:
    """Forward bus events, with a keepalive whenever the bus goes quiet."""
    while True:
        event = await subscription.get(timeout=IDLE_PING_SECONDS)
        if event is None:
            if subscription.closed:
                return
            # The bus is simply quiet. A ping keeps the connection warm through
            # proxies that drop idle sockets, and fails fast on a dead one.
            await websocket.send_json({"type": "ping"})
            continue
        await websocket.send_json(event.to_dict())


@router.websocket("/ws")
async def event_stream(websocket: WebSocket) -> None:
    close_code = _authenticate(websocket)
    if close_code is not None:
        # Accept first: a handshake refused outright surfaces in the browser as a
        # generic 1006 with no reason, so the code would be invisible.
        await websocket.accept()
        await websocket.close(code=close_code)
        return

    bus = getattr(websocket.app.state, "bus", None)
    if bus is None:
        await websocket.accept()
        await websocket.close(code=CLOSE_NOT_READY)
        return

    await websocket.accept()
    subscription = bus.subscribe()
    reader = asyncio.ensure_future(_drain_client(websocket))
    writer = asyncio.ensure_future(_pump(websocket, subscription))
    try:
        await websocket.send_json(
            {"topic": "ws.hello", "data": {"topics": "*", "idle_ping": IDLE_PING_SECONDS}}
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
        log.debug("websocket ended: %s", exc)
    finally:
        for task in (reader, writer):
            task.cancel()
        for task in (reader, writer):
            # CancelledError is a BaseException since 3.8, so it has to be named:
            # awaiting the task we just cancelled is the normal path, and letting
            # it escape would skip the unsubscribe below and leak on the bus.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        subscription.close()
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


@router.get("/ws/info", tags=["ws"])
async def ws_info(
    websocket_user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """What the stream looks like, for a dashboard that suspects it went stale."""
    return {"path": "/api/ws", "idle_ping_seconds": IDLE_PING_SECONDS}


__all__ = ["router"]
