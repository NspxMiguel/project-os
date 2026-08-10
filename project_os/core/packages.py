"""Installing software on the box, from the browser. The Advanced mode.

    "vc tem um computador completo no seu navegador"
    "o cara foi la, foi pro modo advanced, instalo um firefox, um flatpack
     store, e dps voltau pro modo simple. ai aparece em apps."

``installed.py`` answers "what is on this machine". This module is the other
half: **search and install**, which is what makes Advanced a Linux box rather
than a set of admin screens. Whatever gets installed here shows up in Apps
afterwards because ``installed.py`` reads the same package managers back.

Three rules the code keeps:

1. **No shell.** Every command is an argv list, and package names are matched
   against a strict pattern before they reach it. Not because a shell would
   necessarily be exploitable here, but because "the name came from a search
   result" is not a thing worth having to reason about.
2. **Root is asked for honestly.** apt needs it. If the service runs as root,
   fine; if passwordless sudo is available, that is used; otherwise the install
   is refused *before* it starts, with the reason, instead of failing halfway
   through with a permission error in a log nobody reads.
3. **Installs are jobs, not requests.** ``apt-get install`` takes minutes on a
   Pi 3B. Every install gets a job id and a live log, so the screen can show
   what is happening rather than a spinner that might be a hung connection.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from project_os.db import utcnow_iso

log = logging.getLogger(__name__)

SEARCH_TIMEOUT = 25.0
INSTALL_TIMEOUT = 1800.0
#: Kept per job. A long apt run prints thousands of lines and only the tail is
#: ever read; holding all of it is a slow memory leak on a box with 1 GB.
LOG_LINES = 400
MAX_RESULTS = 40

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"

#: A Debian package name, per Debian policy, plus the ``:arch`` suffix apt
#: accepts. Deliberately strict: no spaces, no slashes, no leading dash (which
#: would make the name look like an option to apt).
APT_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]*(:[a-z0-9-]+)?$")
#: A flatpak application id ("org.mozilla.firefox"), or a plain name.
FLATPAK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")


class PackageError(Exception):
    """A refusal with a reason a person can act on."""

    def __init__(self, message: str, code: str = "package_error", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# what this machine can actually do
# ---------------------------------------------------------------------------
def _have(command: str) -> bool:
    return shutil.which(command) is not None


def _is_root() -> bool:
    getuid = getattr(os, "geteuid", None)
    return bool(getuid) and getuid() == 0


#: One answer per command: "may I sudo apt-get" and "may I sudo flatpak" are
#: different questions with different answers.
_sudo_cache = {}  # type: Dict[str, bool]


def reset_privilege_cache() -> None:
    """Forget what sudo said. Used by tests and after a sudoers change."""
    _sudo_cache.clear()


def _sudo_without_password(command: str = "apt-get") -> bool:
    """Whether ``sudo -n`` may run *this command*, asked once per command.

    Asking ``sudo -n true`` instead -- which is what this did -- gets the wrong
    answer on exactly the machine that matters. A well-made sudoers grants the
    few commands the service actually needs and nothing else, so ``true`` is
    denied and the whole package manager concluded it could not become root.
    On the shipped image that meant Advanced mode reported "apt is here, but
    this service cannot become root to use it" while apt was, in fact, allowed.

    ``sudo -l <command>`` asks the real question: may I run that? It exits 0
    when the rule permits it, without running anything.
    """
    if command in _sudo_cache:
        return _sudo_cache[command]
    allowed = False
    if _have("sudo"):
        try:
            result = subprocess.run(
                ["sudo", "-n", "-l", command],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5.0, check=False,
            )
            allowed = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            allowed = False
    _sudo_cache[command] = allowed
    return allowed


def _privilege_prefix(command: str = "apt-get") -> List[str]:
    """[] when already root, ["sudo", "-n"] when that works, else raise."""
    if _is_root():
        return []
    if _sudo_without_password(command):
        return ["sudo", "-n"]
    raise PackageError(
        "Installing system packages needs root, and this service has neither.",
        code="needs_root",
        hint=(
            "Run project-os as a systemd service (the installer does), or give its "
            "user passwordless sudo for apt."
        ),
    )


def backends() -> List[Dict[str, Any]]:
    """Every backend, present or not, each saying why it cannot be used."""
    result = []  # type: List[Dict[str, Any]]

    apt_present = _have("apt-get") and _have("apt-cache")
    apt_reason = ""
    if not apt_present:
        apt_reason = "apt is not on this machine (it is not a Debian-based system)."
    elif not (_is_root() or _sudo_without_password("apt-get")):
        apt_reason = "apt is here, but this service cannot become root to use it."
    result.append({
        "id": "apt",
        "name": "System packages (apt)",
        "present": apt_present,
        "can_install": apt_present and not apt_reason,
        "reason": apt_reason,
        "needs_root": True,
    })

    flatpak_present = _have("flatpak")
    result.append({
        "id": "flatpak",
        "name": "Flatpak",
        "present": flatpak_present,
        "can_install": flatpak_present,
        "reason": "" if flatpak_present else "flatpak is not installed. Install it with apt first.",
        "needs_root": False,
    })
    return result


def available_backends() -> List[str]:
    return [b["id"] for b in backends() if b["can_install"]]


def _require_backend(source: str) -> Dict[str, Any]:
    for backend in backends():
        if backend["id"] == source:
            if not backend["can_install"]:
                raise PackageError(
                    backend["reason"] or "%s cannot be used here." % source,
                    code="backend_unavailable",
                )
            return backend
    raise PackageError("Unknown package source %r." % source, code="unknown_source")


def check_name(source: str, name: str) -> str:
    """The package name, or a refusal. Never reaches a command unchecked."""
    text = str(name or "").strip()
    pattern = APT_NAME if source == "apt" else FLATPAK_NAME
    if not text or not pattern.match(text):
        raise PackageError(
            "%r is not a valid %s package name." % (name, source),
            code="bad_package_name",
        )
    return text


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def _run(argv: List[str], timeout: float = SEARCH_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return ""
    return result.stdout.decode("utf-8", "replace")


def _apt_installed_names() -> set:
    text = _run(["dpkg-query", "-W", "-f=${Package}\\n"])
    return set(line.strip() for line in text.splitlines() if line.strip())


def search_apt(query: str, limit: int = MAX_RESULTS) -> List[Dict[str, Any]]:
    if not (_have("apt-cache")):
        return []
    # --names-only: searching descriptions turns "firefox" into 300 results
    # whose connection to the word is a mention in a changelog.
    text = _run(["apt-cache", "--names-only", "search", query])
    installed = _apt_installed_names()
    found = []  # type: List[Dict[str, Any]]
    for line in text.splitlines():
        if " - " not in line:
            continue
        name, _, summary = line.partition(" - ")
        name = name.strip()
        if not APT_NAME.match(name):
            continue
        found.append({
            "source": "apt",
            "id": name,
            "name": name,
            "summary": summary.strip(),
            "version": "",
            "installed": name in installed,
        })
        if len(found) >= limit:
            break
    return found


def search_flatpak(query: str, limit: int = MAX_RESULTS) -> List[Dict[str, Any]]:
    if not _have("flatpak"):
        return []
    text = _run([
        "flatpak", "search", "--columns=application,name,description,version", query,
    ])
    installed = set()
    listed = _run(["flatpak", "list", "--app", "--columns=application"])
    for line in listed.splitlines():
        if line.strip():
            installed.add(line.strip())
    found = []  # type: List[Dict[str, Any]]
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if not parts or not parts[0] or parts[0].lower().startswith("no matches"):
            continue
        app_id = parts[0]
        if not FLATPAK_NAME.match(app_id):
            continue
        found.append({
            "source": "flatpak",
            "id": app_id,
            "name": parts[1] if len(parts) > 1 else app_id,
            "summary": parts[2] if len(parts) > 2 else "",
            "version": parts[3] if len(parts) > 3 else "",
            "installed": app_id in installed,
        })
        if len(found) >= limit:
            break
    return found


def search(query: str, sources: Optional[List[str]] = None, limit: int = MAX_RESULTS) -> Dict[str, Any]:
    """Search every usable backend. A backend that is missing says so."""
    text = str(query or "").strip()
    if len(text) < 2:
        raise PackageError("Search for at least two characters.", code="query_too_short")

    wanted = sources or ["apt", "flatpak"]
    items = []  # type: List[Dict[str, Any]]
    skipped = []  # type: List[Dict[str, Any]]
    for backend in backends():
        if backend["id"] not in wanted:
            continue
        if not backend["present"]:
            skipped.append({"source": backend["id"], "reason": backend["reason"]})
            continue
        if backend["id"] == "apt":
            items.extend(search_apt(text, limit))
        elif backend["id"] == "flatpak":
            items.extend(search_flatpak(text, limit))
    return {"query": text, "items": items, "count": len(items), "skipped": skipped}


# ---------------------------------------------------------------------------
# install / remove, as jobs
# ---------------------------------------------------------------------------
def install_argv(source: str, name: str) -> List[str]:
    if source == "apt":
        return _privilege_prefix("apt-get") + [
            "apt-get", "install", "-y", "--no-install-recommends", name,
        ]
    if source == "flatpak":
        return ["flatpak", "install", "-y", "--noninteractive", "flathub", name]
    raise PackageError("Unknown package source %r." % source, code="unknown_source")


def remove_argv(source: str, name: str) -> List[str]:
    if source == "apt":
        return _privilege_prefix("apt-get") + ["apt-get", "remove", "-y", name]
    if source == "flatpak":
        return ["flatpak", "uninstall", "-y", "--noninteractive", name]
    raise PackageError("Unknown package source %r." % source, code="unknown_source")


def _environment() -> Dict[str, str]:
    env = dict(os.environ)
    # Without this apt opens a dialog and waits forever for a keypress that is
    # never coming: there is no terminal on the other end of this.
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["LC_ALL"] = "C"
    return env


class Job(object):
    """One install or removal, its log, and where it got to."""

    def __init__(self, action: str, source: str, package: str, argv: List[str]) -> None:
        self.id = uuid.uuid4().hex
        self.action = action
        self.source = source
        self.package = package
        self.argv = argv
        self.state = STATE_QUEUED
        self.message = ""
        self.returncode = None  # type: Optional[int]
        self.created_at = utcnow_iso()
        self.finished_at = ""
        self._log = []  # type: List[str]
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._log.append(line.rstrip("\n"))
            if len(self._log) > LOG_LINES:
                del self._log[: len(self._log) - LOG_LINES]

    def log_lines(self, tail: int = LOG_LINES) -> List[str]:
        with self._lock:
            return list(self._log[-tail:])

    def as_dict(self, tail: int = 40) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "source": self.source,
            "package": self.package,
            "state": self.state,
            "message": self.message,
            "returncode": self.returncode,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "log": self.log_lines(tail),
        }


class JobRunner(object):
    """Runs one package job at a time. Two apt runs at once is a lock error.

    Kept deliberately small: a dict of jobs, a lock, and a thread per run. The
    heavier machinery (a queue in SQLite, retries) would be pretending this is
    something you do fifty times a day.
    """

    def __init__(self, on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        self._jobs = {}  # type: Dict[str, Job]
        self._order = []  # type: List[str]
        self._lock = threading.Lock()
        self._current = None  # type: Optional[str]
        self._on_event = on_event

    # -- reads ----------------------------------------------------------
    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order[-limit:]))
            jobs = [self._jobs[i] for i in ids if i in self._jobs]
        return [job.as_dict(tail=0) for job in jobs]

    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    # -- writes ---------------------------------------------------------
    def start(self, action: str, source: str, package: str) -> Job:
        _require_backend(source)
        name = check_name(source, package)
        argv = install_argv(source, name) if action == "install" else remove_argv(source, name)

        with self._lock:
            if self._current is not None:
                current = self._jobs.get(self._current)
                raise PackageError(
                    "Another package job is still running (%s %s)."
                    % (current.action if current else "?", current.package if current else "?"),
                    code="busy",
                    hint="Wait for it to finish; package managers take one lock at a time.",
                )
            job = Job(action, source, name, argv)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._current = job.id

        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def _emit(self, topic: str, payload: Dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(topic, payload)
        except Exception:  # pragma: no cover - a listener must not kill the job
            log.exception("package job listener failed")

    def _run(self, job: Job) -> None:
        job.state = STATE_RUNNING
        job.append("$ " + " ".join(job.argv))
        self._emit("packages", {"kind": "started", "job": job.as_dict(tail=0)})
        try:
            process = subprocess.Popen(
                job.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=_environment(),
                bufsize=1,
                universal_newlines=True,
            )
        except OSError as exc:
            job.state = STATE_ERROR
            job.message = str(exc)
            job.finished_at = utcnow_iso()
            self._finish(job)
            return

        try:
            if process.stdout is not None:
                for line in process.stdout:
                    job.append(line)
            process.wait(timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            job.state = STATE_ERROR
            job.message = "Timed out after %d minutes." % int(INSTALL_TIMEOUT / 60)
            job.finished_at = utcnow_iso()
            self._finish(job)
            return
        except Exception as exc:  # pragma: no cover - defensive
            process.kill()
            job.state = STATE_ERROR
            job.message = str(exc)
            job.finished_at = utcnow_iso()
            self._finish(job)
            return

        job.returncode = process.returncode
        if process.returncode == 0:
            job.state = STATE_DONE
            job.message = "%s %s: done." % (job.action, job.package)
        else:
            job.state = STATE_ERROR
            tail = [line for line in job.log_lines(12) if line.strip()]
            job.message = tail[-1] if tail else "Exited with status %d." % process.returncode
        job.finished_at = utcnow_iso()
        self._finish(job)

    def _finish(self, job: Job) -> None:
        with self._lock:
            if self._current == job.id:
                self._current = None
        log.info("package job %s %s %s -> %s", job.action, job.source, job.package, job.state)
        self._emit("packages", {"kind": "finished", "job": job.as_dict(tail=5)})
        # The apps list is built from what the package managers report, so it is
        # stale the moment this finishes.
        try:
            from project_os.core import installed as installed_core

            installed_core.reset_cache()
        except Exception:  # pragma: no cover
            log.debug("could not reset the installed-apps cache", exc_info=True)


__all__ = [
    "INSTALL_TIMEOUT",
    "Job",
    "JobRunner",
    "PackageError",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "available_backends",
    "backends",
    "check_name",
    "install_argv",
    "remove_argv",
    "search",
    "search_apt",
    "search_flatpak",
]
