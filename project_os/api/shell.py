"""The terminal.

> "nas configuracoes, em configurações do dev, adiciona um botao pra adicioanr o
> terminal ali no canto, q serve pra qualquer coisa ne, baixa apps e etc"

Two ways in, for two different jobs:

* ``POST /shell/exec`` runs one command and returns what it printed. This is what
  install recipes and the "run this for me" buttons use.
* ``WS /shell/ws`` is a real pseudo-terminal: colours, ``htop``, ``nano``, Ctrl-C,
  a password prompt that does not echo. This is what the docked terminal panel
  attaches to.

**Both are off until someone turns them on.** ``security.allow_shell`` defaults
to false, and there is no honest way to soften what turning it on means: a shell
is the whole machine, so anyone who reaches a logged-in browser tab has the
machine. The settings screen says exactly that, in those words, rather than a
reassuring sentence that would be false.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import shutil
import signal
import struct
import termios
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from project_os import auth, paths
from project_os.errors import ApiError
from project_os.main import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/shell", tags=["shell"])

DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
#: Truncate captured output here. A command that prints 200 MB (``cat`` on the
#: wrong file) must not take the server down with it.
MAX_OUTPUT = 256 * 1024
READ_CHUNK = 8192

CLOSE_UNAUTHENTICATED = 4401
CLOSE_DISABLED = 4403
CLOSE_NO_PTY = 4501

try:  # pragma: no cover - platform check
    import pty as _pty
except ImportError:  # pragma: no cover - Windows
    _pty = None


class ExecBody(BaseModel):
    command: str = Field(..., min_length=1)
    cwd: Optional[str] = None
    timeout: float = Field(DEFAULT_TIMEOUT, gt=0, le=MAX_TIMEOUT)


def _require_shell(config: Any) -> None:
    if not config.get("security.allow_shell", False):
        raise ApiError(
            403,
            "shell_disabled",
            "The terminal is off. Turn on security.allow_shell in Settings > Developer.",
        )


def _login_shell() -> str:
    return os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"


def _environment() -> Dict[str, str]:
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["PROJECT_OS_HOME"] = str(paths.home())
    # Stop programs from trying to open a graphical password prompt on a box
    # with no display; they hang forever instead of failing.
    env.pop("DISPLAY", None)
    env["SUDO_ASKPASS"] = ""
    return env


@router.get("/status")
async def status(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    """Whether the terminal can be used, and honestly why not when it cannot."""
    enabled = bool(config.get("security.allow_shell", False))
    return {
        "enabled": enabled,
        "pty_available": _pty is not None,
        "shell": _login_shell(),
        "websocket": "/api/shell/ws",
        "reason": None
        if enabled
        else "O security.allow_shell está desligado. Ligue em Configurações › Desenvolvedor.",
    }


@router.post("/exec")
async def execute(
    body: ExecBody,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Run one command and return its output.

    Runs through the shell, so pipes and redirection work -- which is the point,
    and also why this endpoint is behind ``allow_shell``. There is no list of
    "safe" commands here, because such a list has never survived contact with
    ``sh -c``.
    """
    _require_shell(config)
    cwd = body.cwd or str(paths.home())
    if not os.path.isdir(cwd):
        raise ApiError(400, "bad_cwd", "%s is not a directory." % cwd)

    try:
        process = await asyncio.create_subprocess_shell(
            body.command,
            cwd=cwd,
            env=_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise ApiError(500, "spawn_failed", "Could not start a shell: %s" % exc)

    timed_out = False
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), body.timeout)
    except asyncio.TimeoutError:
        timed_out = True
        stdout = b""
        # Kill the whole process group: the timeout is usually a child that is
        # hanging, and killing only the shell would orphan it.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()

    text = (stdout or b"").decode("utf-8", "replace")
    truncated = len(text) > MAX_OUTPUT
    if truncated:
        text = text[:MAX_OUTPUT] + "\n... (saída cortada)"
    log.info("shell exec by %s: %s", user.get("username"), body.command[:200])
    return {
        "command": body.command,
        "cwd": cwd,
        "exit_code": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "truncated": truncated,
        "output": text,
    }


# ---------------------------------------------------------------------------
# the docked terminal
# ---------------------------------------------------------------------------
def _set_size(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(OSError, ValueError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class _Session(object):
    """One pseudo-terminal, its child process, and the socket it is bound to."""

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.pid = -1
        self.fd = -1
        self.rows = rows
        self.cols = cols

    def spawn(self, cwd: str) -> None:
        pid, fd = _pty.fork()
        if pid == 0:  # pragma: no cover - the child never returns
            try:
                os.chdir(cwd)
            except OSError:
                pass
            for key, value in _environment().items():
                os.environ[key] = value
            os.execvp(_login_shell(), [_login_shell(), "-l"])
            os._exit(1)
        self.pid = pid
        self.fd = fd
        os.set_blocking(fd, False)
        _set_size(fd, self.rows, self.cols)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = max(1, rows), max(1, cols)
        _set_size(self.fd, self.rows, self.cols)

    def write(self, data: str) -> None:
        with contextlib.suppress(OSError):
            os.write(self.fd, data.encode("utf-8", "replace"))

    def close(self) -> None:
        # SIGHUP, then SIGKILL: a shell given a hangup runs its normal exit path
        # and takes its children with it, which SIGKILL alone would not.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(self.pid), signal.SIGHUP)
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(self.pid), signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(self.pid, os.WNOHANG)


def _authorise(websocket: WebSocket) -> Optional[int]:
    config = getattr(websocket.app.state, "config", None)
    if config is None or not config.get("security.allow_shell", False):
        return CLOSE_DISABLED
    if _pty is None:
        return CLOSE_NO_PTY
    if not auth.auth_enabled(websocket):
        return None
    db = getattr(websocket.app.state, "db", None)
    if db is None or auth.setup_required(db):
        return CLOSE_UNAUTHENTICATED
    if auth.current_user(websocket) is not None:
        return None
    token = websocket.query_params.get("token")
    if token and auth.session_user(db, token) is not None:
        return None
    return CLOSE_UNAUTHENTICATED


@router.websocket("/ws")
async def terminal(websocket: WebSocket) -> None:
    """A live shell for the docked terminal panel.

    Client frames are JSON: ``{"type":"input","data":"ls\\n"}`` and
    ``{"type":"resize","rows":40,"cols":120}``. Server frames are
    ``{"type":"output","data":...}`` and a final ``{"type":"exit"}``.
    """
    refusal = _authorise(websocket)
    if refusal is not None:
        await websocket.accept()
        await websocket.close(code=refusal)
        return

    await websocket.accept()
    session = _Session()
    loop = asyncio.get_running_loop()
    outgoing = asyncio.Queue()  # type: asyncio.Queue

    def _on_readable() -> None:
        try:
            chunk = os.read(session.fd, READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            # The child exited and closed its side of the pty.
            outgoing.put_nowait(None)
            return
        if not chunk:
            outgoing.put_nowait(None)
            return
        outgoing.put_nowait(chunk.decode("utf-8", "replace"))

    try:
        session.spawn(str(paths.home()))
    except OSError as exc:
        await websocket.send_json({"type": "error", "message": "Could not open a shell: %s" % exc})
        await websocket.close(code=CLOSE_NO_PTY)
        return

    loop.add_reader(session.fd, _on_readable)
    log.info("terminal session opened (pid %s)", session.pid)

    async def _pump_out() -> None:
        while True:
            item = await outgoing.get()
            if item is None:
                await websocket.send_json({"type": "exit"})
                return
            await websocket.send_json({"type": "output", "data": item})

    async def _pump_in() -> None:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "input":
                session.write(str(message.get("data", "")))
            elif kind == "resize":
                session.resize(int(message.get("rows", 24)), int(message.get("cols", 80)))

    writer = asyncio.ensure_future(_pump_out())
    reader = asyncio.ensure_future(_pump_in())
    try:
        await websocket.send_json({"type": "ready", "shell": _login_shell(), "pid": session.pid})
        await asyncio.wait([reader, writer], return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, ConnectionError):
        pass
    except Exception:  # pragma: no cover - defensive
        log.debug("terminal session failed", exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(session.fd)
        for task in (reader, writer):
            task.cancel()
        for task in (reader, writer):
            with contextlib.suppress(Exception):
                await task
        session.close()
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
        log.info("terminal session closed (pid %s)", session.pid)


__all__ = ["router"]
