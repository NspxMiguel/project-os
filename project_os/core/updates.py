"""Updating project-os from project-os.

    "faz um sistema tbm, de atualizar o sistema direto pelo sistema. ou seja,
     n precisa fica tirando o pendrive, puxa pela rede, tlvz até um dominio
     nosso"

No SD card leaving the Pi, no reflashing, no laptop. The box fetches its own new
version over the network and restarts into it.

Two ways in, picked automatically, because the two ways project-os gets onto a
machine are different:

* **git** -- what ``install.sh`` does (a clone in ``/opt/project-os``). Updating is
  a fetch and a hard reset onto the tracked branch or a tag. Cheap, and it keeps
  working if the release host is down.
* **tarball** -- a JSON manifest at a URL says what the latest version is and
  where its tarball lives, with a sha256. That is the "domínio nosso" path: the
  manifest can be a file on any web server, GitHub Releases included.

Whichever it is, the shape is the same and the rules do not change:

1. **Nothing is applied that was not verified.** A tarball whose sha256 does not
   match the manifest is deleted, not installed. Skipping this would make every
   update an invitation to whoever can answer for the update host.
2. **The old tree is kept.** The swap is: extract beside, move current aside,
   move new in. If the new version does not boot, the previous one is still on
   disk, and :func:`rollback` puts it back.
3. **State is never touched.** ``PROJECT_OS_HOME`` (database, config, media) lives
   outside the code tree on purpose; an update replaces code and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from project_os import __version__

log = logging.getLogger(__name__)

#: Where the manifest lives when nobody configured anything. A raw file in the
#: public repo: no release infrastructure needed to ship the first update, and it
#: is a plain URL, so pointing this at a domain later changes one setting.
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/NspxMiguel/project-os/main/release/latest.json"
)

NETWORK_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 600.0
GIT_TIMEOUT = 120.0

METHOD_GIT = "git"
METHOD_TARBALL = "tarball"

#: The systemd unit the image installs. Spelled once, here, because the last
#: time it was spelled inline it kept the pre-rename underscore and every
#: update silently failed to restart anything.
UNIT_NAME = "project-os.service"

#: Never inside the code tree, and never removed by an update.
KEEP_IN_PLACE = (".venv", "PEDIDOS.md")


class UpdateError(Exception):
    def __init__(self, message: str, code: str = "update_failed", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# where the code is
# ---------------------------------------------------------------------------
def root_dir() -> str:
    """The directory holding the ``project_os`` package -- the thing we replace."""
    import project_os

    package = os.path.dirname(os.path.abspath(project_os.__file__))
    return os.path.dirname(package)


def is_git_checkout(root: Optional[str] = None) -> bool:
    return os.path.isdir(os.path.join(root or root_dir(), ".git"))


def _git(args: List[str], root: Optional[str] = None, timeout: float = GIT_TIMEOUT) -> str:
    where = root or root_dir()
    try:
        result = subprocess.run(
            ["git", "-C", where] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("git failed: %s" % exc, code="git_failed")
    if result.returncode != 0:
        raise UpdateError(
            result.stderr.decode("utf-8", "replace").strip() or "git exited %d" % result.returncode,
            code="git_failed",
        )
    return result.stdout.decode("utf-8", "replace").strip()


def method(root: Optional[str] = None) -> str:
    """How this install updates itself."""
    where = root or root_dir()
    if is_git_checkout(where):
        try:
            if _git(["remote"], where):
                return METHOD_GIT
        except UpdateError:
            pass
    return METHOD_TARBALL


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []  # type: List[int]
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(candidate: str, current: str = __version__) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------
def _fetch_json(url: str, timeout: float = NETWORK_TIMEOUT) -> Dict[str, Any]:
    # urllib rather than httpx: checking for updates must work on a box where
    # the optional extras were never installed.
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "project-os/%s" % __version__})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raise UpdateError(
            "The update server answered %d." % exc.code, code="manifest_http_error",
            hint="Check updates.manifest_url in Settings.",
        )
    except URLError as exc:
        raise UpdateError(
            "Could not reach the update server: %s" % exc.reason, code="offline",
            hint="This box needs internet access to check for updates.",
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("The update manifest is not valid JSON.", code="bad_manifest") from exc


def check_tarball(manifest_url: str) -> Dict[str, Any]:
    manifest = _fetch_json(manifest_url)
    version = str(manifest.get("version") or "").strip()
    url = str(manifest.get("url") or "").strip()
    sha256 = str(manifest.get("sha256") or "").strip().lower()
    if not version or not url:
        raise UpdateError(
            "The manifest is missing \"version\" or \"url\".", code="bad_manifest",
        )
    if not sha256:
        raise UpdateError(
            "The manifest has no sha256, so the download cannot be verified.",
            code="unverifiable",
            hint="Every release must publish a checksum; refusing to install without one.",
        )
    return {
        "method": METHOD_TARBALL,
        "current": __version__,
        "latest": version,
        "update_available": is_newer(version),
        "url": url,
        "sha256": sha256,
        "notes": str(manifest.get("notes") or ""),
        "published_at": str(manifest.get("published_at") or ""),
        "checked_at": time.time(),
    }


def check_git(branch: str = "main", root: Optional[str] = None) -> Dict[str, Any]:
    where = root or root_dir()
    _git(["fetch", "--quiet", "--tags", "origin", branch], where)
    local = _git(["rev-parse", "HEAD"], where)
    remote = _git(["rev-parse", "origin/%s" % branch], where)
    behind = "0"
    if local != remote:
        behind = _git(["rev-list", "--count", "HEAD..origin/%s" % branch], where) or "0"
    subject = _git(["log", "-1", "--pretty=%s", "origin/%s" % branch], where)
    return {
        "method": METHOD_GIT,
        "current": __version__,
        "latest": remote[:12],
        "update_available": local != remote,
        "commits_behind": int(behind or 0),
        "branch": branch,
        "notes": subject,
        "checked_at": time.time(),
    }


def check(manifest_url: str = DEFAULT_MANIFEST_URL, branch: str = "main",
          root: Optional[str] = None) -> Dict[str, Any]:
    if method(root) == METHOD_GIT:
        return check_git(branch, root)
    return check_tarball(manifest_url)


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------
def _download(url: str, dest: str, expected_sha256: str,
              on_line: Optional[Any] = None, timeout: float = DOWNLOAD_TIMEOUT) -> str:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "project-os/%s" % __version__})
    digest = hashlib.sha256()
    total = 0
    try:
        with urlopen(request, timeout=timeout) as response, open(dest, "wb") as handle:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    except (HTTPError, URLError, OSError) as exc:
        raise UpdateError("Download failed: %s" % exc, code="download_failed")

    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        os.remove(dest)
        raise UpdateError(
            "The download does not match the checksum in the manifest.",
            code="checksum_mismatch",
            hint="Nothing was installed. Either the release is corrupt or it is not the one advertised.",
        )
    if on_line:
        on_line("downloaded %.1f MB, sha256 ok" % (total / 1048576.0))
    return actual


def _safe_extract(archive: str, into: str) -> str:
    """Extract, refusing any member that would land outside ``into``.

    A tarball is a list of paths chosen by whoever built it. ``../../etc/cron.d``
    is a valid path; refusing it here is the difference between an update and a
    remote write anywhere on the disk.
    """
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        base = os.path.realpath(into)
        for member in members:
            target = os.path.realpath(os.path.join(into, member.name))
            if target != base and not target.startswith(base + os.sep):
                raise UpdateError(
                    "The archive tries to write outside the install directory (%s)." % member.name,
                    code="unsafe_archive",
                )
            if member.issym() or member.islnk():
                link = os.path.realpath(os.path.join(os.path.dirname(target), member.linkname))
                if link != base and not link.startswith(base + os.sep):
                    raise UpdateError(
                        "The archive contains a link pointing outside (%s)." % member.name,
                        code="unsafe_archive",
                    )
        tar.extractall(into)

    entries = [name for name in os.listdir(into) if not name.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(into, entries[0])):
        # GitHub-style tarballs wrap everything in project-os-1.2.3/.
        return os.path.join(into, entries[0])
    return into


def _looks_like_project_os(path: str) -> bool:
    return os.path.isdir(os.path.join(path, "project_os")) and os.path.isfile(
        os.path.join(path, "project_os", "__init__.py")
    )


def apply_tarball(info: Dict[str, Any], root: Optional[str] = None,
                  on_line: Optional[Any] = None) -> Dict[str, Any]:
    """Download, verify, and swap the code tree. Returns where the old one went."""
    where = os.path.abspath(root or root_dir())
    say = on_line or (lambda line: None)

    parent = os.path.dirname(where)
    workdir = tempfile.mkdtemp(prefix=".project_os-update-", dir=parent)
    try:
        archive = os.path.join(workdir, "release.tar.gz")
        say("fetching %s" % info["url"])
        _download(info["url"], archive, info["sha256"], on_line=say)

        say("extracting")
        extracted = _safe_extract(archive, os.path.join(workdir, "tree"))
        if not _looks_like_project_os(extracted):
            raise UpdateError(
                "The archive does not contain a project-os tree.", code="bad_archive",
            )

        # Anything that must survive the swap is carried across rather than
        # restored afterwards: a half-applied update is worse than none.
        for name in KEEP_IN_PLACE:
            source = os.path.join(where, name)
            if os.path.exists(source) and not os.path.exists(os.path.join(extracted, name)):
                say("keeping %s" % name)
                if os.path.isdir(source):
                    shutil.move(source, os.path.join(extracted, name))
                else:
                    shutil.copy2(source, os.path.join(extracted, name))

        previous = "%s.previous-%s" % (where, info.get("current") or __version__)
        if os.path.exists(previous):
            shutil.rmtree(previous, ignore_errors=True)
        say("swapping in %s" % info.get("latest", "the new version"))
        os.rename(where, previous)
        try:
            os.rename(extracted, where)
        except OSError:
            os.rename(previous, where)  # put it back before giving up
            raise
        say("done -- previous version kept at %s" % previous)
        return {"previous": previous, "root": where}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def apply_git(info: Dict[str, Any], root: Optional[str] = None,
              on_line: Optional[Any] = None) -> Dict[str, Any]:
    where = root or root_dir()
    say = on_line or (lambda line: None)
    branch = info.get("branch") or "main"
    before = _git(["rev-parse", "HEAD"], where)
    say("fetching origin/%s" % branch)
    _git(["fetch", "--quiet", "--tags", "origin", branch], where)
    say("resetting to origin/%s" % branch)
    _git(["reset", "--quiet", "--hard", "origin/%s" % branch], where)
    after = _git(["rev-parse", "HEAD"], where)
    say("now at %s" % after[:12])
    return {"previous": before, "root": where}


def rollback(previous: str, root: Optional[str] = None) -> None:
    """Put a tarball update back the way it was."""
    where = os.path.abspath(root or root_dir())
    if not os.path.isdir(previous):
        raise UpdateError("There is no previous version at %s." % previous, code="no_previous")
    broken = "%s.failed" % where
    shutil.rmtree(broken, ignore_errors=True)
    if os.path.exists(where):
        os.rename(where, broken)
    os.rename(previous, where)
    shutil.rmtree(broken, ignore_errors=True)


# ---------------------------------------------------------------------------
# dependencies and restart
# ---------------------------------------------------------------------------
def venv_python(root: Optional[str] = None) -> Optional[str]:
    candidate = os.path.join(root or root_dir(), ".venv", "bin", "python3")
    return candidate if os.path.isfile(candidate) else None


def install_requirements(root: Optional[str] = None, on_line: Optional[Any] = None) -> int:
    """Bring the venv in line with the new requirements.txt. Best effort.

    A failure here is reported but does not fail the update: the new code is
    already in place, and a missing optional dependency degrades honestly
    everywhere in project-os by design.
    """
    where = root or root_dir()
    say = on_line or (lambda line: None)
    requirements = os.path.join(where, "requirements.txt")
    python = venv_python(where) or sys.executable
    if not os.path.isfile(requirements):
        say("no requirements.txt; skipping")
        return 0
    say("installing dependencies")
    process = subprocess.Popen(
        [python, "-m", "pip", "install", "--quiet", "--upgrade", "-r", requirements],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, universal_newlines=True,
    )
    if process.stdout is not None:
        for line in process.stdout:
            say(line.rstrip())
    return process.wait()


def under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID")) or os.path.exists("/run/systemd/system")


def restart(on_line: Optional[Any] = None) -> str:
    """Restart into the new code, whichever way this process is supervised.

    Under systemd the unit is restarted and this process is expected to die. Run
    by hand, it re-executes itself -- which is the same thing minus the
    supervisor, and it means the update works during development too.
    """
    say = on_line or (lambda line: None)
    if under_systemd() and shutil.which("systemctl"):
        # The unit is ``project-os.service``. It was ``project_os`` before the
        # rename, and restarting a unit that does not exist fails quietly: the
        # update reported success and the box went on serving the old code until
        # somebody happened to reboot it.
        say("reiniciando o serviço project-os")
        subprocess.Popen(["systemctl", "restart", UNIT_NAME])
        return "systemd"
    argv = restart_argv()
    # The swap renamed the directory this process is sitting in, and a working
    # directory follows the inode, not the name: without this chdir the restart
    # comes back up inside `<root>.previous-<version>` -- old code, new
    # everything else, and a version number that never changes no matter how
    # many times you update. Chdir by path re-resolves onto the new tree.
    where = root_dir()
    try:
        os.chdir(where)
        say("working directory: %s" % where)
    except OSError as exc:  # pragma: no cover - the tree we just installed
        say("could not chdir to %s: %s" % (where, exc))
    say("re-executing: %s" % " ".join(argv))
    os.execv(argv[0], argv)
    return "exec"  # pragma: no cover - execv does not return


def restart_argv(argv: Optional[List[str]] = None, executable: Optional[str] = None) -> List[str]:
    """The command line to come back up with.

    ``python -m project_os`` leaves ``sys.argv[0]`` as the path to
    ``project_os/__main__.py``. Re-executing *that* path is not the same command:
    Python then puts ``project_os/`` itself on sys.path instead of its parent, so
    ``import project_os`` fails unless the package also happens to be installed in
    the venv. Restarting has to be the one thing that always works, so ``-m`` is
    reconstructed.
    """
    original = list(argv if argv is not None else _original_argv())
    python = executable or sys.executable
    first = original[0] if original else ""
    if os.path.basename(first) == "__main__.py":
        package = os.path.basename(os.path.dirname(os.path.abspath(first))) or "project_os"
        return [python, "-m", package] + original[1:]
    return [python] + original


_argv_snapshot = None  # type: Optional[List[str]]


def remember_argv(argv: Optional[List[str]] = None) -> None:
    """Called once at boot, before anything has a chance to mutate sys.argv."""
    global _argv_snapshot
    _argv_snapshot = list(argv if argv is not None else sys.argv)


def _original_argv() -> List[str]:
    return list(_argv_snapshot if _argv_snapshot is not None else sys.argv)


__all__ = [
    "DEFAULT_MANIFEST_URL",
    "METHOD_GIT",
    "METHOD_TARBALL",
    "UpdateError",
    "apply_git",
    "apply_tarball",
    "check",
    "check_git",
    "check_tarball",
    "install_requirements",
    "is_git_checkout",
    "is_newer",
    "method",
    "remember_argv",
    "restart",
    "restart_argv",
    "rollback",
    "root_dir",
    "under_systemd",
]
