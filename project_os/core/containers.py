"""The container runtime: docker or podman, whichever the machine happens to have.

    "O project-os em si nunca roda em contêiner. [...] App grande de terceiro
    roda em contêiner [...] O motor de contêiner não vem instalado [...] Ele é
    instalado sob demanda."

(``docs/CONCEITO.md``, section 9.) project-os is never the thing inside a
container -- this module only ever runs *other* people's images, on behalf of
a catalog entry that asked for one. A clean box has neither docker nor podman,
and that has to be an ordinary, nameable state rather than a crash: every
function here degrades to "no runtime" instead of raising when neither binary
is on PATH, so the store can say so before anyone clicks install.

Everything a catalog entry supplies -- image, ports, volumes, env -- is data
from a YAML file, possibly from a repository someone added by hand. It is
validated here into a plain dict *before* it ever becomes part of an argument
list, and it only ever becomes an argument list: nothing in this module builds
a shell string or passes ``shell=True``. A malformed or hostile spec fails
:func:`parse_spec` with a clear reason instead of reaching ``subprocess``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: Tried in this order; docker is by far the more common install, so it wins
#: when a machine somehow has both.
RUNTIMES = ("docker", "podman")

DEFAULT_TIMEOUT = 30.0
#: A cold pull of a real image (Jellyfin, n8n, ...) over a home connection can
#: take minutes; the store already tells the user to expect that.
PULL_TIMEOUT = 900.0
STOP_TIMEOUT = 30.0

#: Every container project-os manages carries this prefix, so `docker ps -a`
#: on a shared box makes it obvious which containers are ours.
NAME_PREFIX = "project_os-"

_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_VOLUME_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTAINER_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]*$")
#: Image refs: registry/name:tag or name@sha256:digest. Deliberately
#: conservative -- this is about to become the last element of an argv, not
#: about supporting every syntax the docs allow.
_IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$")


class ContainerError(Exception):
    """The runtime is unusable, or a spec cannot be trusted, or a command failed."""


# ---------------------------------------------------------------------------
# runtime detection
# ---------------------------------------------------------------------------
def detect_runtime() -> Optional[str]:
    """``"docker"``, ``"podman"``, or ``None`` if neither is on PATH."""
    for name in RUNTIMES:
        if shutil.which(name):
            return name
    return None


def runtime_status() -> Dict[str, Any]:
    """What the store shows before anyone clicks install."""
    engine = detect_runtime()
    return {
        "available": engine is not None,
        "engine": engine,
        "reason": None
        if engine is not None
        else "No container runtime is installed (looked for %s)." % " or ".join(RUNTIMES),
    }


def require_runtime() -> str:
    engine = detect_runtime()
    if engine is None:
        raise ContainerError(
            "No container runtime is installed (looked for %s)." % " or ".join(RUNTIMES)
        )
    return engine


# ---------------------------------------------------------------------------
# spec validation -- catalog data in, a safe argv-ready dict out
# ---------------------------------------------------------------------------
def _port(value: Any, label: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ContainerError("%s port %r is not a number" % (label, value))
    if not (1 <= port <= 65535):
        raise ContainerError("%s port %r is outside 1-65535" % (label, value))
    return port


def parse_spec(app_id: str, raw: Any) -> Dict[str, Any]:
    """Validate a catalog entry's ``container:`` block.

    Called on every install, not just once at catalog load, because the
    catalog can be re-read from disk (or come from a repository) between the
    two. Raises :class:`ContainerError` with a reason a person could act on;
    never lets a bad field reach a command line.
    """
    if not _APP_ID_RE.match(app_id or ""):
        raise ContainerError("%r is not a usable app id" % (app_id,))
    if not isinstance(raw, dict):
        raise ContainerError("the container spec must be a mapping")

    image = str(raw.get("image") or "").strip()
    if not image or not _IMAGE_RE.match(image) or ".." in image:
        raise ContainerError("%r is not a usable image reference" % (image,))

    ports = []  # type: List[Dict[str, int]]
    for item in raw.get("ports") or []:
        if not isinstance(item, dict):
            raise ContainerError("each port entry needs host and container numbers")
        ports.append(
            {
                "host": _port(item.get("host"), "host"),
                "container": _port(item.get("container"), "container"),
            }
        )

    volumes = []  # type: List[Dict[str, str]]
    seen_names = set()  # type: set
    for item in raw.get("volumes") or []:
        if not isinstance(item, dict):
            raise ContainerError("each volume entry needs a name and a path")
        name = str(item.get("name") or "").strip()
        if not _VOLUME_NAME_RE.match(name):
            raise ContainerError("%r is not a usable volume name" % (name,))
        if name in seen_names:
            raise ContainerError("volume name %r is used twice" % (name,))
        seen_names.add(name)
        path = str(item.get("path") or "").strip()
        if not _CONTAINER_PATH_RE.match(path):
            raise ContainerError("%r is not a usable container path" % (path,))
        mode = str(item.get("mode") or "rw").strip().lower()
        if mode not in ("rw", "ro"):
            raise ContainerError("volume mode must be rw or ro, not %r" % (mode,))
        volumes.append({"name": name, "path": path, "mode": mode})

    env = {}  # type: Dict[str, str]
    raw_env = raw.get("env") or {}
    if not isinstance(raw_env, dict):
        raise ContainerError("env must be a mapping of NAME: value")
    for key, value in raw_env.items():
        key = str(key)
        if not _ENV_KEY_RE.match(key):
            raise ContainerError("%r is not a usable environment variable name" % (key,))
        env[key] = str(value)

    return {"image": image, "ports": ports, "volumes": volumes, "env": env}


def container_name(app_id: str) -> str:
    return NAME_PREFIX + app_id


# ---------------------------------------------------------------------------
# running commands
# ---------------------------------------------------------------------------
def _run(argv: List[str], timeout: float = DEFAULT_TIMEOUT) -> "subprocess.CompletedProcess[bytes]":
    """Run one command with an argument list. Never a shell string.

    Every caller in this module builds ``argv`` from :func:`parse_spec`'s
    already-validated output, so there is nothing here left to escape.
    """
    try:
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ContainerError("%s is not installed" % argv[0])
    except subprocess.TimeoutExpired:
        raise ContainerError("%s took longer than %ds" % (" ".join(argv[:2]), int(timeout)))
    except OSError as exc:  # pragma: no cover - exotic exec failures
        raise ContainerError("could not run %s: %s" % (argv[0], exc))


def _check(argv: List[str], timeout: float = DEFAULT_TIMEOUT, action: str = "run it") -> str:
    result = _run(argv, timeout)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        tail = stderr.splitlines()[-1] if stderr else "exit code %d" % result.returncode
        raise ContainerError("could not %s: %s" % (action, tail))
    return result.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# argv building
# ---------------------------------------------------------------------------
def create_argv(engine: str, app_id: str, spec: Dict[str, Any], data_dir: Path) -> List[str]:
    """The ``create`` command for this spec. Pure, so it is easy to test
    without a runtime: give it a spec and a directory, get back a list."""
    name = container_name(app_id)
    argv = [engine, "create", "--name", name, "--restart", "unless-stopped"]
    for port in spec["ports"]:
        argv += ["-p", "%d:%d" % (port["host"], port["container"])]
    for volume in spec["volumes"]:
        host_path = data_dir / volume["name"]
        argv += ["-v", "%s:%s:%s" % (str(host_path), volume["path"], volume["mode"])]
    for key, value in spec["env"].items():
        argv += ["-e", "%s=%s" % (key, value)]
    argv.append(spec["image"])
    return argv


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def image_present(engine: str, image: str) -> bool:
    return _run([engine, "image", "inspect", image]).returncode == 0


def pull(engine: str, image: str) -> None:
    _check([engine, "pull", image], timeout=PULL_TIMEOUT, action="pull %s" % image)


def inspect(engine: str, app_id: str) -> Optional[Dict[str, Any]]:
    """The runtime's own view of the container, or ``None`` if it does not exist."""
    name = container_name(app_id)
    result = _run([engine, "inspect", name])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:  # pragma: no cover - a runtime lying about its own output
        return None
    return data[0] if data else None


def container_status(engine: str, app_id: str) -> str:
    """``"missing" | "running" | "exited" | ...`` -- whatever the runtime calls it."""
    info = inspect(engine, app_id)
    if info is None:
        return "missing"
    state = info.get("State") or {}
    if state.get("Running"):
        return "running"
    return str(state.get("Status") or "unknown")


def status_detail(engine: str, app_id: str) -> Dict[str, Any]:
    info = inspect(engine, app_id)
    if info is None:
        return {"container_status": "missing"}
    state = info.get("State") or {}
    config = info.get("Config") or {}
    return {
        "container_status": "running" if state.get("Running") else str(state.get("Status") or "unknown"),
        "started_at": state.get("StartedAt"),
        "image": config.get("Image"),
    }


def ensure_running(engine: str, app_id: str, spec: Dict[str, Any], data_dir: Path) -> None:
    """Pull if needed, create if needed, start. Safe to call more than once.

    This is what both "install" and "start again after a reboot" call: an app
    that is already created and running should not be re-created, and one
    that exists but is stopped should not be re-pulled.
    """
    name = container_name(app_id)
    if not image_present(engine, spec["image"]):
        pull(engine, spec["image"])
    if inspect(engine, app_id) is None:
        for volume in spec["volumes"]:
            (data_dir / volume["name"]).mkdir(parents=True, exist_ok=True)
        _check(create_argv(engine, app_id, spec, data_dir), action="create %s" % name)
    _check([engine, "start", name], action="start %s" % name)


def stop(engine: str, app_id: str, timeout: float = STOP_TIMEOUT) -> None:
    name = container_name(app_id)
    if inspect(engine, app_id) is None:
        return
    _check(
        [engine, "stop", "-t", str(int(timeout)), name],
        timeout=timeout + DEFAULT_TIMEOUT,
        action="stop %s" % name,
    )


def remove(engine: str, app_id: str, force: bool = True) -> None:
    """Remove the container. Volumes under the app's data directory are left
    alone -- uninstalling an app keeping its data is the rule everywhere else
    in the store, and a container app is not an exception."""
    name = container_name(app_id)
    if inspect(engine, app_id) is None:
        return
    argv = [engine, "rm"]
    if force:
        argv.append("-f")
    argv.append(name)
    _check(argv, action="remove %s" % name)


def logs(engine: str, app_id: str, tail: int = 200) -> str:
    name = container_name(app_id)
    result = _run([engine, "logs", "--tail", str(int(tail)), name])
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")


__all__ = [
    "ContainerError",
    "DEFAULT_TIMEOUT",
    "PULL_TIMEOUT",
    "RUNTIMES",
    "container_name",
    "container_status",
    "create_argv",
    "detect_runtime",
    "ensure_running",
    "image_present",
    "inspect",
    "logs",
    "parse_spec",
    "pull",
    "remove",
    "require_runtime",
    "runtime_status",
    "status_detail",
    "stop",
]
