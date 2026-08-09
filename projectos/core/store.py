"""The app store: repositories, catalogue merging, install and uninstall.

A *repository* is ``{"name": ..., "url": ...}`` in ``apps.repositories``. Four
URL shapes are understood::

    builtin://core                  apps bundled with the wheel
    file:///srv/apps/index.json     a local index (or a directory holding one)
    https://host/apps/index.json    a remote index
    https://host/user/repo.git      a git repository holding an index

An index is ``{"apps": [{id, name, version, ..., download: {...}}]}`` (a bare
list works too). Exactly one repository ships by default -- ``builtin://core``,
which touches no network at all. Nothing is fetched from anywhere the user did
not configure.

Installing means: download into a temporary directory *inside* the apps
directory (so the final move is a rename, not a copy across filesystems),
validate the manifest, then swap it into place atomically and ask the
:class:`~projectos.core.plugins.PluginManager` to load it. Progress is
published on the ``app.state`` topic.

Archives are treated as hostile: zip-slip, absolute paths, symlinks, device
nodes, lying size headers and entry-count bombs are all rejected before a
single byte lands on the SD card.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from projectos import __version__, paths
from projectos.core.plugins import APP_ID_RE, ManifestError, validate_manifest
from projectos.errors import ApiError

log = logging.getLogger(__name__)

#: The only repository configured out of the box.
DEFAULT_REPOSITORY = {"name": "builtin", "url": "builtin://core"}

#: How long a fetched index stays fresh.
CACHE_TTL_SECONDS = 300.0

#: Ceilings. A Pi 3B installs onto an SD card; an app that needs more than this
#: is not something this box should be unpacking behind a spinner.
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000

HTTP_TIMEOUT = 30.0
GIT_TIMEOUT = 600.0

#: Where an index lives inside a git repository, in order of preference.
INDEX_FILENAMES = ("index.json", "catalog.json", "apps/index.json", "apps/catalog.json")

DOWNLOAD_TYPES = ("git", "zip", "builtin", "dir")

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>a|alpha|b|beta|c|rc|pre|preview)[-_.]?(?P<pre_num>\d+)?)?"
    r"(?:[-_.]?(?P<post>post|rev|r)[-_.]?(?P<post_num>\d+)?)?"
    r"(?:[-_.]?(?P<dev>dev)[-_.]?(?P<dev_num>\d+)?)?"
    r"(?:\+[0-9A-Za-z.\-]+)?\s*$"
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

# Stage ordering: a dev build precedes its pre-releases, which precede the
# final release, which precedes any post-release of the same version.
_STAGE_DEV = 0
_STAGE_PRE = 1
_STAGE_FINAL = 2
_STAGE_POST = 3


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
def parse_version(text: Any) -> Tuple[Tuple[int, ...], Tuple[int, int, int]]:
    """``"1.2.3rc1"`` -> ``((1, 2, 3), (1, 2, 1))``.

    Handles the shapes real manifests use -- semver, PEP 440 and the sloppy
    middle ground (``v1.2``, ``1.2.3-beta.2``, ``2.0.0+build7``) -- without
    pulling in ``packaging``. Anything unparseable degrades to the numbers it
    contains rather than raising.
    """
    raw = str(text if text is not None else "").strip()
    match = _VERSION_RE.match(raw)
    if match is None:
        numbers = re.findall(r"\d+", raw)
        release = tuple(int(n) for n in numbers[:6]) if numbers else (0,)
        return release, (_STAGE_FINAL, 0, 0)

    release = tuple(int(part) for part in match.group("release").split("."))
    pre, post, dev = match.group("pre"), match.group("post"), match.group("dev")
    if pre:
        stage = (
            _STAGE_PRE,
            _PRE_RANK.get(pre.lower(), 2),
            int(match.group("pre_num") or 0),
        )
    elif post:
        stage = (_STAGE_POST, int(match.group("post_num") or 0), 0)
    elif dev:
        stage = (_STAGE_DEV, int(match.group("dev_num") or 0), 0)
    else:
        stage = (_STAGE_FINAL, 0, 0)
    return release, stage


def compare_versions(left: Any, right: Any) -> int:
    """``-1`` / ``0`` / ``1``. ``1.0`` and ``1.0.0`` compare equal."""
    left_release, left_stage = parse_version(left)
    right_release, right_stage = parse_version(right)
    width = max(len(left_release), len(right_release))
    padded_left = left_release + (0,) * (width - len(left_release))
    padded_right = right_release + (0,) * (width - len(right_release))
    if padded_left != padded_right:
        return -1 if padded_left < padded_right else 1
    if left_stage != right_stage:
        return -1 if left_stage < right_stage else 1
    return 0


def version_newer(candidate: Any, current: Any) -> bool:
    return compare_versions(candidate, current) > 0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _is_url(text: str) -> bool:
    return bool(_SCHEME_RE.match(text)) or text.startswith("git@")


def repository_kind(url: str) -> str:
    """``builtin`` | ``git`` | ``file`` | ``http`` for a repository URL."""
    value = str(url or "").strip()
    lowered = value.lower()
    if lowered.startswith("builtin://"):
        return "builtin"
    if (
        lowered.startswith("git+")
        or lowered.startswith("git://")
        or lowered.startswith("ssh://")
        or value.startswith("git@")
        or lowered.endswith(".git")
    ):
        return "git"
    if lowered.startswith("file://"):
        return "file"
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "http"
    if not _SCHEME_RE.match(value) and value:
        # A bare path is a local index.
        return "file"
    return "unknown"


def _git_url(url: str) -> str:
    """Strip the ``git+`` prefix pip-style URLs carry."""
    return url[4:] if url.lower().startswith("git+") else url


def _local_path(url: str) -> Path:
    if url.lower().startswith("file://"):
        parsed = urlparse(url)
        return Path(unquote(parsed.path or ""))
    return Path(url)


def _name_from_url(url: str) -> str:
    value = str(url or "").strip()
    if value.lower().startswith("builtin://"):
        return value[len("builtin://") :] or "builtin"
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\.(json|git|zip)$", "", tail, flags=re.IGNORECASE)
    return tail or "repository"


def _id_from_url(url: str) -> str:
    """Best guess at an app id, used only until the manifest says otherwise."""
    candidate = _name_from_url(url).lower().replace(" ", "-")
    candidate = re.sub(r"[^a-z0-9_-]", "", candidate)
    return candidate if APP_ID_RE.match(candidate or "") else ""


def _rmtree(path: Path) -> None:
    shutil.rmtree(str(path), ignore_errors=True)


def _read_json_bytes(payload: bytes, where: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(502, "invalid_index", "%s is not valid JSON: %s" % (where, exc))


def _lenient_manifest(directory: Path) -> Optional[Dict[str, Any]]:
    """Read an installed app's manifest without judging it.

    A bundled app that needs a newer core, or one with a broken entrypoint,
    still has to show up as *installed* -- otherwise the store offers to
    install something that is already sitting on disk.
    """
    manifest_file = directory / "manifest.json"
    try:
        if not manifest_file.is_file():
            return None
        data = json.loads(manifest_file.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("unreadable manifest at %s: %s", manifest_file, exc)
        return None
    if not isinstance(data, dict):
        return None
    app_id = str(data.get("id") or directory.name).strip()
    if not APP_ID_RE.match(app_id):
        return None
    data["id"] = app_id
    data.setdefault("name", app_id)
    data.setdefault("version", "0.0.0")
    return data


# ---------------------------------------------------------------------------
# archive safety
# ---------------------------------------------------------------------------
def _reject_member_name(name: str) -> None:
    if not name or name in (".", "./"):
        raise ApiError(400, "unsafe_archive", "The archive contains an unnamed entry.")
    if "\x00" in name:
        raise ApiError(400, "unsafe_archive", "The archive contains a NUL byte in a path.")
    if "\\" in name:
        raise ApiError(
            400, "unsafe_archive", "The archive contains a backslash path: %s" % name
        )
    if name.startswith("/") or _WINDOWS_DRIVE_RE.match(name):
        raise ApiError(
            400, "unsafe_archive", "The archive contains an absolute path: %s" % name
        )
    if any(part == ".." for part in name.split("/")):
        raise ApiError(
            400, "unsafe_archive", "The archive tries to escape its directory: %s" % name
        )


def safe_extract_zip(
    archive: Path,
    dest: Path,
    max_bytes: int = MAX_UNPACKED_BYTES,
    max_entries: int = MAX_ARCHIVE_ENTRIES,
) -> int:
    """Extract ``archive`` into ``dest``, refusing anything hostile.

    Rejects zip-slip (``..``, absolute and drive-letter paths), symlinks and
    device nodes, more than ``max_entries`` members, and more than
    ``max_bytes`` of output -- checked both against the declared sizes *and*
    while writing, because a zip header is just a claim.

    Returns the number of bytes written.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    try:
        handle = zipfile.ZipFile(str(archive))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ApiError(400, "invalid_archive", "That download is not a usable zip: %s" % exc)

    written = 0
    with handle:
        infos = handle.infolist()
        if len(infos) > max_entries:
            raise ApiError(
                413,
                "archive_too_large",
                "The archive has %d entries; the limit is %d." % (len(infos), max_entries),
            )
        declared = sum(int(info.file_size or 0) for info in infos)
        if declared > max_bytes:
            raise ApiError(
                413,
                "archive_too_large",
                "The archive unpacks to %d bytes; the limit is %d." % (declared, max_bytes),
            )

        for info in infos:
            name = info.filename
            _reject_member_name(name)
            mode = (int(info.external_attr) >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ApiError(
                    400, "unsafe_archive", "The archive contains a symlink: %s" % name
                )
            if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ApiError(
                    400,
                    "unsafe_archive",
                    "The archive contains something that is not a file: %s" % name,
                )
            target = (dest / name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ApiError(
                    400, "unsafe_archive", "The archive tries to write outside itself: %s" % name
                )

        for info in infos:
            target = dest / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source:
                with open(str(target), "wb") as out:
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise ApiError(
                                413,
                                "archive_too_large",
                                "The archive unpacks to more than %d bytes." % max_bytes,
                            )
                        out.write(chunk)
    return written


def find_app_root(root: Path) -> Path:
    """Locate the directory holding ``manifest.json``.

    Release archives and git forges wrap everything in one top-level directory
    (``repo-main/``); both shapes resolve to the same place here.
    """
    if (root / "manifest.json").is_file():
        return root
    try:
        children = [child for child in sorted(root.iterdir()) if child.is_dir()]
    except OSError as exc:  # pragma: no cover - unreadable temp dir
        raise ApiError(500, "install_failed", "Cannot read the download: %s" % exc)
    candidates = [child for child in children if (child / "manifest.json").is_file()]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates and len(children) == 1:
        return find_app_root(children[0])
    if len(candidates) > 1:
        raise ApiError(
            400,
            "ambiguous_download",
            "That download holds %d apps; install them one at a time." % len(candidates),
        )
    raise ApiError(400, "invalid_app", "No manifest.json was found in that download.")


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def _urlopen_bytes(url: str, limit: int, timeout: float) -> bytes:
    """Blocking fetch with the stdlib. Only ever called in an executor."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, headers={"User-Agent": "ProjectOS/%s" % __version__, "Accept": "*/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            chunks = []  # type: List[bytes]
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ApiError(
                        413, "download_too_large", "That download is larger than %d bytes." % limit
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise ApiError(502, "repository_unreachable", "%s returned HTTP %s." % (url, exc.code))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApiError(502, "repository_unreachable", "Cannot reach %s: %s" % (url, exc))


async def fetch_bytes(url: str, limit: int = MAX_DOWNLOAD_BYTES, timeout: float = HTTP_TIMEOUT) -> bytes:
    """Download a URL, capped at ``limit`` bytes.

    Uses ``httpx`` when it is installed (streaming, properly async) and falls
    back to ``urllib`` in a worker thread so a minimal install still works.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https", "file"):
        raise ApiError(400, "unsupported_url", "%r is not a downloadable URL." % url)

    if scheme in ("http", "https"):
        try:
            import httpx
        except ImportError:
            httpx = None  # type: ignore[assignment]
        if httpx is not None:
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async with client.stream(
                        "GET", url, headers={"User-Agent": "ProjectOS/%s" % __version__}
                    ) as response:
                        if response.status_code >= 400:
                            raise ApiError(
                                502,
                                "repository_unreachable",
                                "%s returned HTTP %d." % (url, response.status_code),
                            )
                        chunks = []  # type: List[bytes]
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise ApiError(
                                    413,
                                    "download_too_large",
                                    "That download is larger than %d bytes." % limit,
                                )
                            chunks.append(chunk)
                        return b"".join(chunks)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(502, "repository_unreachable", "Cannot reach %s: %s" % (url, exc))

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _urlopen_bytes, url, limit, timeout)


def _git_env() -> Dict[str, str]:
    env = dict(os.environ)
    # Never let a clone sit waiting for a password on a headless box.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return env


async def git_clone(url: str, dest: Path, ref: Optional[str] = None, timeout: float = GIT_TIMEOUT) -> None:
    """Shallow-clone ``url`` into ``dest`` (which must not exist yet)."""
    args = ["git", "clone", "--depth", "1", "--single-branch", "--quiet"]
    if ref:
        args += ["--branch", str(ref)]
    args += ["--", _git_url(url), str(dest)]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
    except FileNotFoundError:
        raise ApiError(
            501,
            "git_missing",
            "git is not installed. Install it with: sudo apt install git",
        )
    except OSError as exc:  # pragma: no cover - exotic exec failures
        raise ApiError(500, "git_failed", "Could not run git: %s" % exc)

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        with _suppress_process_errors():
            process.kill()
            await process.wait()
        raise ApiError(504, "git_timeout", "Cloning %s took longer than %ds." % (url, int(timeout)))

    if process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = message[-1] if message else "git exited with %s" % process.returncode
        raise ApiError(502, "git_failed", "Could not clone %s: %s" % (url, tail))


class _suppress_process_errors(object):
    def __enter__(self) -> "_suppress_process_errors":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, (OSError, ProcessLookupError))


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class AppStore(object):
    """Merges repositories into one catalogue and installs from it."""

    def __init__(
        self,
        config: Any,
        plugins: Any = None,
        bus: Any = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.plugins = plugins
        self.bus = bus
        self.log = logger or log
        self.cache_ttl = CACHE_TTL_SECONDS
        self._cache = {}  # type: Dict[str, Tuple[float, List[Dict[str, Any]]]]
        self._lock = None  # type: Optional[asyncio.Lock]

    # -- plumbing --------------------------------------------------------
    def _get_lock(self) -> asyncio.Lock:
        # Bound to whichever loop is running when it is first needed; the store
        # may well be constructed before there is one.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _emit(
        self,
        app_id: str,
        state: str,
        message: str = "",
        name: str = "",
        error: Optional[str] = None,
        progress: Optional[float] = None,
        enabled: Optional[bool] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish on ``app.state``; never let the bus break an install."""
        if self.bus is None:
            return
        payload = {
            "id": app_id,
            "state": state,
            "name": name or app_id,
            "error": error,
            "message": message,
            "source": "store",
        }
        if progress is not None:
            payload["progress"] = round(float(progress), 3)
        if enabled is not None:
            payload["enabled"] = bool(enabled)
        if extra:
            payload.update(extra)
        try:
            self.bus.publish_nowait("app.state", payload)
        except Exception:  # pragma: no cover
            self.log.debug("publishing app.state failed", exc_info=True)

    def invalidate_cache(self) -> None:
        self._cache = {}

    # -- repositories ----------------------------------------------------
    def repositories(self) -> List[Dict[str, Any]]:
        """The configured repositories, normalised and deduplicated."""
        raw = self.config.get("apps.repositories", None)
        if not isinstance(raw, list) or not raw:
            raw = [dict(DEFAULT_REPOSITORY)]
        out = []  # type: List[Dict[str, Any]]
        seen = set()  # type: set
        for item in raw:
            if isinstance(item, str):
                item = {"name": _name_from_url(item), "url": item}
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(
                {
                    "name": str(item.get("name") or _name_from_url(url)).strip(),
                    "url": url,
                    "kind": repository_kind(url),
                    "enabled": item.get("enabled", True) is not False,
                }
            )
        return out

    def _persist_repositories(self, repositories: Sequence[Dict[str, Any]]) -> None:
        stored = []  # type: List[Dict[str, Any]]
        for repo in repositories:
            entry = {"name": repo["name"], "url": repo["url"]}
            if repo.get("enabled") is False:
                entry["enabled"] = False
            stored.append(entry)
        self.config.set("apps.repositories", stored)
        self.config.save()
        self.invalidate_cache()

    def add_repository(self, name: str, url: str) -> Dict[str, Any]:
        url = str(url or "").strip()
        name = str(name or "").strip() or _name_from_url(url)
        if not url:
            raise ApiError(400, "invalid_repository", "A repository needs a URL.")
        kind = repository_kind(url)
        if kind == "unknown":
            raise ApiError(
                400,
                "invalid_repository",
                "Use builtin://, file://, http(s):// or a git URL.",
            )
        repositories = self.repositories()
        for repo in repositories:
            if repo["url"] == url:
                raise ApiError(409, "repository_exists", "That repository is already configured.")
            if repo["name"].lower() == name.lower():
                raise ApiError(409, "repository_exists", "A repository is already called %r." % name)
        entry = {"name": name, "url": url, "kind": kind, "enabled": True}
        repositories.append(entry)
        self._persist_repositories(repositories)
        self.log.info("added app repository %s (%s)", name, url)
        return entry

    def remove_repository(self, name_or_url: str) -> Dict[str, Any]:
        needle = str(name_or_url or "").strip()
        if not needle:
            raise ApiError(400, "invalid_repository", "Name the repository to remove.")
        repositories = self.repositories()
        for index, repo in enumerate(repositories):
            if repo["url"] != needle and repo["name"].lower() != needle.lower():
                continue
            if repo["kind"] == "builtin":
                raise ApiError(
                    400,
                    "repository_protected",
                    "The builtin repository lists the apps shipped with ProjectOS; it cannot be removed.",
                )
            repositories.pop(index)
            self._persist_repositories(repositories)
            self.log.info("removed app repository %s (%s)", repo["name"], repo["url"])
            return repo
        raise ApiError(404, "repository_not_found", "No repository called %r." % needle)

    # -- installed state -------------------------------------------------
    def installed_map(self) -> Dict[str, Dict[str, Any]]:
        """``{app_id: {version, source, path, name}}`` read from disk.

        Disk, not the plugin manager: this is the thing install and uninstall
        actually have to be right about.
        """
        found = {}  # type: Dict[str, Dict[str, Any]]
        for source, directory in (
            ("builtin", paths.builtin_apps_dir()),
            ("user", paths.apps_dir()),
        ):
            try:
                if not directory.is_dir():
                    continue
                entries = sorted(directory.iterdir())
            except OSError as exc:  # pragma: no cover
                self.log.warning("cannot read %s: %s", directory, exc)
                continue
            for entry in entries:
                if not entry.is_dir() or entry.name.startswith((".", "_")):
                    continue
                manifest = _lenient_manifest(entry)
                if manifest is None:
                    continue
                found[manifest["id"]] = {
                    "id": manifest["id"],
                    "name": manifest.get("name", manifest["id"]),
                    "version": str(manifest.get("version", "0.0.0")),
                    "source": source,
                    "path": str(entry),
                    "manifest": manifest,
                }
        return found

    async def installed_map_async(self) -> Dict[str, Dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.installed_map)

    def is_bundled(self, app_id: str) -> bool:
        return (paths.builtin_apps_dir() / app_id).is_dir()

    # -- catalogue -------------------------------------------------------
    def _normalise_entry(self, raw: Any, repo: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        app_id = str(raw.get("id") or "").strip()
        if not APP_ID_RE.match(app_id):
            self.log.warning("repository %s lists an entry with an unusable id %r", repo["name"], app_id)
            return None
        download = raw.get("download")
        if not isinstance(download, dict):
            download = {"type": "builtin"} if repo["kind"] == "builtin" else {}
        download_type = str(download.get("type") or "").strip().lower()
        if not download_type:
            url = str(download.get("url") or "")
            download_type = "git" if repository_kind(url) == "git" else ("zip" if url else "builtin")
        entry = {
            "id": app_id,
            "name": str(raw.get("name") or app_id),
            "version": str(raw.get("version") or "0.0.0"),
            "description": str(raw.get("description") or raw.get("summary") or ""),
            "author": str(raw.get("author") or ""),
            "icon": str(raw.get("icon") or "\U0001f4e6"),
            "category": str(raw.get("category") or "other"),
            "homepage": str(raw.get("homepage") or ""),
            "download": {
                "type": download_type,
                "url": str(download.get("url") or ""),
                "ref": download.get("ref") or None,
            },
            "min_projectos": str(raw.get("min_projectos") or "0.0.0"),
            "permissions": list(raw.get("permissions") or []),
            "repository": repo["name"],
            "repository_url": repo["url"],
        }
        # Pass-through fields the service-app catalogue (docs/APPS.md) adds, so
        # a richer repository is not flattened on its way to the UI.
        for optional in ("kind", "requires", "license", "screenshots", "tags"):
            if optional in raw:
                entry[optional] = raw[optional]
        return entry

    def _builtin_entries(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries = []  # type: List[Dict[str, Any]]
        directory = paths.builtin_apps_dir()
        try:
            if not directory.is_dir():
                return entries
            children = sorted(directory.iterdir())
        except OSError as exc:  # pragma: no cover
            raise ApiError(500, "repository_unreadable", "Cannot read %s: %s" % (directory, exc))
        for child in children:
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            manifest = _lenient_manifest(child)
            if manifest is None:
                continue
            raw = dict(manifest)
            raw["download"] = {"type": "builtin"}
            entry = self._normalise_entry(raw, repo)
            if entry is not None:
                entry["bundled"] = True
                entries.append(entry)
        return entries

    def _file_index(self, repo: Dict[str, Any]) -> bytes:
        path = _local_path(repo["url"])
        if path.is_dir():
            for candidate in INDEX_FILENAMES:
                target = path / candidate
                if target.is_file():
                    path = target
                    break
        try:
            if not path.is_file():
                raise ApiError(404, "repository_missing", "No index at %s." % path)
            if path.stat().st_size > MAX_INDEX_BYTES:
                raise ApiError(413, "index_too_large", "%s is too large to be an index." % path)
            return path.read_bytes()
        except OSError as exc:
            raise ApiError(502, "repository_unreadable", "Cannot read %s: %s" % (path, exc))

    async def _git_index(self, repo: Dict[str, Any]) -> bytes:
        parent = Path(tempfile.mkdtemp(prefix="projectos-repo-"))
        clone = parent / "repo"
        try:
            await git_clone(repo["url"], clone)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._read_cloned_index, clone, repo)
        finally:
            await asyncio.get_running_loop().run_in_executor(None, _rmtree, parent)

    def _read_cloned_index(self, clone: Path, repo: Dict[str, Any]) -> bytes:
        for candidate in INDEX_FILENAMES:
            target = clone / candidate
            if target.is_file():
                try:
                    return target.read_bytes()
                except OSError as exc:  # pragma: no cover
                    raise ApiError(502, "repository_unreadable", "Cannot read %s: %s" % (target, exc))
        raise ApiError(
            502,
            "repository_missing",
            "%s has no index.json (looked for %s)." % (repo["url"], ", ".join(INDEX_FILENAMES)),
        )

    async def _fetch_entries(self, repo: Dict[str, Any]) -> List[Dict[str, Any]]:
        kind = repo["kind"]
        if kind == "builtin":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._builtin_entries, repo)
        if kind == "file":
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(None, self._file_index, repo)
        elif kind == "http":
            payload = await fetch_bytes(repo["url"], MAX_INDEX_BYTES)
        elif kind == "git":
            payload = await self._git_index(repo)
        else:
            raise ApiError(400, "invalid_repository", "Unsupported repository URL: %s" % repo["url"])

        data = _read_json_bytes(payload, repo["url"])
        raw_apps = data.get("apps") if isinstance(data, dict) else data
        if not isinstance(raw_apps, list):
            raise ApiError(502, "invalid_index", "%s does not list any apps." % repo["url"])
        entries = []  # type: List[Dict[str, Any]]
        for raw in raw_apps:
            entry = self._normalise_entry(raw, repo)
            if entry is not None:
                entries.append(entry)
        return entries

    async def entries_for(self, repo: Dict[str, Any], refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch one repository's entries, memoised for :attr:`cache_ttl`."""
        cached = self._cache.get(repo["url"])
        if not refresh and cached is not None and (time.monotonic() - cached[0]) < self.cache_ttl:
            return cached[1]
        entries = await self._fetch_entries(repo)
        self._cache[repo["url"]] = (time.monotonic(), entries)
        return entries

    async def catalog(self, refresh: bool = False) -> Dict[str, Any]:
        """Every repository merged into one list, plus per-repository status."""
        installed = await self.installed_map_async()
        merged = {}  # type: Dict[str, Dict[str, Any]]
        status = []  # type: List[Dict[str, Any]]

        for repo in self.repositories():
            record = {
                "name": repo["name"],
                "url": repo["url"],
                "kind": repo["kind"],
                "enabled": repo["enabled"],
                "ok": True,
                "error": None,
                "count": 0,
            }
            if not repo["enabled"]:
                status.append(record)
                continue
            try:
                entries = await self.entries_for(repo, refresh)
            except ApiError as exc:
                record.update({"ok": False, "error": exc.message})
                self.log.warning("repository %s failed: %s", repo["name"], exc.message)
                status.append(record)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                record.update({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
                self.log.warning("repository %s failed", repo["name"], exc_info=True)
                status.append(record)
                continue
            record["count"] = len(entries)
            status.append(record)
            for entry in entries:
                current = merged.get(entry["id"])
                if current is None or compare_versions(entry["version"], current["version"]) > 0:
                    merged[entry["id"]] = entry

        for app_id, info in installed.items():
            if app_id in merged:
                continue
            # Installed but no repository lists it any more: still show it, so
            # the store can offer to remove it instead of pretending it is gone.
            merged[app_id] = {
                "id": app_id,
                "name": info["name"],
                "version": info["version"],
                "description": str(info["manifest"].get("description") or ""),
                "author": str(info["manifest"].get("author") or ""),
                "icon": str(info["manifest"].get("icon") or "\U0001f4e6"),
                "category": str(info["manifest"].get("category") or "other"),
                "homepage": str(info["manifest"].get("homepage") or ""),
                "download": {"type": "builtin" if info["source"] == "builtin" else "", "url": "", "ref": None},
                "min_projectos": str(info["manifest"].get("min_projectos") or "0.0.0"),
                "permissions": list(info["manifest"].get("permissions") or []),
                "repository": None,
                "repository_url": None,
            }

        apps = [self._annotate(entry, installed) for entry in merged.values()]
        apps.sort(key=lambda item: (item.get("category") or "", item["name"].lower()))
        return {
            "apps": apps,
            "repositories": status,
            "installed_count": len(installed),
            "projectos_version": __version__,
        }

    def _annotate(self, entry: Dict[str, Any], installed: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        item = dict(entry)
        record = installed.get(item["id"])
        bundled = bool(item.get("bundled")) or (record is not None and record["source"] == "builtin")
        item["bundled"] = bundled
        item["installed"] = record is not None
        item["installed_version"] = record["version"] if record else None
        item["installed_source"] = record["source"] if record else None
        item["update_available"] = bool(
            record is not None
            and item["download"].get("type") not in ("", "builtin")
            and version_newer(item["version"], record["version"])
        )
        compatible = compare_versions(item.get("min_projectos", "0.0.0"), __version__) <= 0
        item["compatible"] = compatible
        item["incompatible_reason"] = (
            None
            if compatible
            else "Needs ProjectOS %s or newer (this is %s)." % (item["min_projectos"], __version__)
        )
        item["installable"] = compatible and item["download"].get("type") in ("git", "zip", "dir")
        item["removable"] = record is not None and record["source"] == "user"
        return item

    async def available(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Every app the configured repositories offer, marked up with state."""
        return (await self.catalog(refresh))["apps"]

    async def find(self, app_id: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
        for entry in await self.available(refresh):
            if entry["id"] == app_id:
                return entry
        return None

    # -- install ---------------------------------------------------------
    def _entry_from_url(self, url: str, ref: Optional[str]) -> Dict[str, Any]:
        kind = repository_kind(url)
        lowered = url.lower()
        if kind == "git":
            download_type = "git"
        elif lowered.endswith(".zip"):
            download_type = "zip"
        elif kind == "file" and _local_path(url).is_dir():
            download_type = "dir"
        elif kind in ("http", "file"):
            download_type = "zip"
        else:
            raise ApiError(
                400,
                "unsupported_url",
                "Install from a git URL, a .zip, or a local directory.",
            )
        return {
            "id": _id_from_url(url),
            "name": _name_from_url(url),
            "version": "0.0.0",
            "download": {"type": download_type, "url": url, "ref": ref},
            "repository": None,
            "repository_url": None,
            "min_projectos": "0.0.0",
        }

    def _guard_target(self, app_id: str, installed: Dict[str, Dict[str, Any]], update: bool) -> bool:
        """Returns True when this install replaces something already there."""
        record = installed.get(app_id)
        if self.is_bundled(app_id) and not update:
            raise ApiError(
                409,
                "app_id_conflict",
                "%r is the id of an app that ships with ProjectOS. Installing it would "
                "shadow the bundled one; retry with update=true if that is what you want." % app_id,
            )
        if record is not None and record["source"] == "user" and not update:
            raise ApiError(
                409,
                "already_installed",
                "%s %s is already installed. Retry with update=true to replace it."
                % (record["name"], record["version"]),
            )
        return record is not None

    async def install(
        self,
        id_or_url: str,
        ref: Optional[str] = None,
        update: bool = False,
        enable: bool = True,
    ) -> Dict[str, Any]:
        """Install an app by catalogue id or by URL.

        Downloads into a staging directory next to the final one, validates the
        manifest, swaps it in with a rename, and asks the plugin manager to
        load it. A failure anywhere leaves the previous installation in place.
        """
        target = str(id_or_url or "").strip()
        if not target:
            raise ApiError(400, "invalid_request", "Say what to install.")

        async with self._get_lock():
            if _is_url(target):
                entry = self._entry_from_url(target, ref)
            else:
                if not APP_ID_RE.match(target):
                    raise ApiError(400, "invalid_app_id", "%r is not a valid app id." % target)
                found = await self.find(target)
                if found is None:
                    raise ApiError(
                        404,
                        "app_not_in_store",
                        "No repository offers an app called %r." % target,
                    )
                entry = found
                if ref:
                    entry = dict(entry)
                    entry["download"] = dict(entry["download"])
                    entry["download"]["ref"] = ref

            download = entry.get("download") or {}
            download_type = str(download.get("type") or "").lower()
            if download_type == "builtin":
                raise ApiError(
                    400,
                    "already_bundled",
                    "%s ships with ProjectOS; enable it instead of installing it." % entry["id"],
                )
            if download_type not in ("git", "zip", "dir"):
                raise ApiError(
                    400,
                    "unsupported_download",
                    "Do not know how to install a %r download." % (download_type or "missing"),
                )

            installed = await self.installed_map_async()
            expected_id = entry.get("id") or ""
            if expected_id:
                self._guard_target(expected_id, installed, update)
                if not entry.get("compatible", True):
                    raise ApiError(409, "incompatible_app", entry.get("incompatible_reason") or "Incompatible app.")

            label = expected_id or entry.get("name") or target
            self._emit(label, "installing", "Downloading…", name=entry.get("name", label), progress=0.1, enabled=False)

            apps_root = paths.apps_dir()
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=str(apps_root)))
            loop = asyncio.get_running_loop()
            try:
                app_root = await self._materialise(entry, staging)
                manifest = await loop.run_in_executor(None, self._validate_downloaded, app_root)
                app_id = manifest["id"]

                if expected_id and app_id != expected_id:
                    raise ApiError(
                        409,
                        "app_id_mismatch",
                        "The download calls itself %r, not %r." % (app_id, expected_id),
                    )
                if not expected_id:
                    self._guard_target(app_id, installed, update)

                self._emit(
                    app_id,
                    "installing",
                    "Installing…",
                    name=manifest.get("name", app_id),
                    progress=0.75,
                    enabled=False,
                )
                await loop.run_in_executor(None, self._swap_into_place, app_root, apps_root / app_id)
            except BaseException as exc:
                await loop.run_in_executor(None, _rmtree, staging)
                message = exc.message if isinstance(exc, ApiError) else "%s: %s" % (type(exc).__name__, exc)
                self._emit(label, "install_failed", message, name=entry.get("name", label), error=message, enabled=False)
                if isinstance(exc, (ApiError, asyncio.CancelledError)):
                    raise
                self.log.exception("installing %s failed", label)
                raise ApiError(500, "install_failed", message)
            else:
                await loop.run_in_executor(None, _rmtree, staging)

            self.invalidate_cache()
            replaced = app_id in installed
            self.log.info(
                "installed app %s %s (%s)", app_id, manifest.get("version"), "update" if replaced else "new"
            )

            app_info = await self._load_installed(app_id, enable)
            self._emit(
                app_id,
                "installed",
                "Installed",
                name=manifest.get("name", app_id),
                progress=1.0,
                enabled=enable,
                extra={"version": str(manifest.get("version", "0.0.0"))},
            )
            return {
                "id": app_id,
                "name": manifest.get("name", app_id),
                "version": str(manifest.get("version", "0.0.0")),
                "installed": True,
                "updated": replaced,
                "enabled": bool(enable),
                "app": app_info,
            }

    async def _materialise(self, entry: Dict[str, Any], staging: Path) -> Path:
        """Get the app's files into ``staging`` and return the app root."""
        download = entry["download"]
        download_type = str(download.get("type")).lower()
        url = str(download.get("url") or "")
        ref = download.get("ref")
        loop = asyncio.get_running_loop()
        unpacked = staging / "unpacked"

        if download_type == "git":
            if not url:
                raise ApiError(400, "invalid_download", "That entry has no git URL.")
            await git_clone(url, unpacked, ref)
            # A shallow clone still carries a .git directory; the app is code we
            # own now, and an SD card has better uses for those blocks.
            await loop.run_in_executor(None, _rmtree, unpacked / ".git")
        elif download_type == "zip":
            if not url:
                raise ApiError(400, "invalid_download", "That entry has no download URL.")
            payload = await fetch_bytes(url, MAX_DOWNLOAD_BYTES)
            archive = staging / "download.zip"
            await loop.run_in_executor(None, archive.write_bytes, payload)
            await loop.run_in_executor(None, safe_extract_zip, archive, unpacked)
            await loop.run_in_executor(None, _remove_file, archive)
        elif download_type == "dir":
            source = _local_path(url)
            if not source.is_dir():
                raise ApiError(404, "download_missing", "No directory at %s." % source)
            await loop.run_in_executor(None, _copy_tree, source, unpacked)
        else:  # pragma: no cover - guarded by the caller
            raise ApiError(400, "unsupported_download", "Unsupported download type %r." % download_type)

        return await loop.run_in_executor(None, find_app_root, unpacked)

    def _validate_downloaded(self, app_root: Path) -> Dict[str, Any]:
        manifest_file = app_root / "manifest.json"
        try:
            raw = json.loads(manifest_file.read_text("utf-8"))
        except OSError as exc:
            raise ApiError(400, "invalid_app", "Cannot read manifest.json: %s" % exc)
        except ValueError as exc:
            raise ApiError(400, "invalid_app", "manifest.json is not valid JSON: %s" % exc)
        try:
            return validate_manifest(raw)
        except ManifestError as exc:
            raise ApiError(400, "invalid_app", "That app's manifest is not usable: %s" % exc)

    def _swap_into_place(self, source: Path, final: Path) -> None:
        """Rename ``source`` onto ``final``, keeping the old copy until it works."""
        final.parent.mkdir(parents=True, exist_ok=True)
        backup = final.parent / (".backup-%s-%s" % (final.name, os.urandom(4).hex()))
        had_previous = final.exists()
        if had_previous:
            os.replace(str(final), str(backup))
        try:
            os.replace(str(source), str(final))
        except OSError as exc:
            if had_previous:
                try:
                    os.replace(str(backup), str(final))
                except OSError:  # pragma: no cover - both moves failing is fatal
                    self.log.error("could not restore %s after a failed install", final)
            raise ApiError(500, "install_failed", "Could not move the app into place: %s" % exc)
        finally:
            if had_previous:
                _rmtree(backup)

    async def _load_installed(self, app_id: str, enable: bool) -> Optional[Dict[str, Any]]:
        """Hand the freshly installed directory to the plugin manager."""
        if self.plugins is None:
            return None
        try:
            await self.plugins.load_all()
            if enable:
                await self.plugins.enable(app_id)
            return self.plugins.info(app_id)
        except ApiError:
            raise
        except Exception as exc:
            # The files are on disk and valid; a failure to start is the app's
            # problem, not the installer's, and the plugin manager records it.
            self.log.warning("app %s installed but did not load: %s", app_id, exc)
            return None

    # -- uninstall -------------------------------------------------------
    async def uninstall(self, app_id: str, keep_data: bool = True) -> Dict[str, Any]:
        """Remove a user-installed app. Bundled apps are refused."""
        app_id = str(app_id or "").strip()
        if not APP_ID_RE.match(app_id):
            raise ApiError(400, "invalid_app_id", "%r is not a valid app id." % app_id)

        async with self._get_lock():
            target = paths.apps_dir() / app_id
            if not target.is_dir():
                if self.is_bundled(app_id):
                    raise ApiError(
                        400,
                        "app_bundled",
                        "%s ships with ProjectOS and cannot be uninstalled. Disable it instead." % app_id,
                    )
                raise ApiError(404, "app_not_installed", "%s is not installed." % app_id)

            name = app_id
            manifest = _lenient_manifest(target)
            if manifest:
                name = str(manifest.get("name") or app_id)

            if self.plugins is not None:
                try:
                    await self.plugins.disable(app_id)
                except ApiError as exc:
                    if exc.status != 404:
                        raise
                except Exception as exc:  # pragma: no cover - a stubborn app
                    self.log.warning("app %s did not stop cleanly: %s", app_id, exc)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _rmtree, target)
            if not keep_data:
                try:
                    await loop.run_in_executor(None, _rmtree, paths.data_dir(app_id))
                except ValueError:  # pragma: no cover - id already validated
                    pass

            self._forget(app_id)
            self.invalidate_cache()
            if self.plugins is not None:
                try:
                    # A bundled app of the same id, previously shadowed by this
                    # one, comes back here.
                    await self.plugins.load_all()
                except Exception:  # pragma: no cover
                    self.log.warning("reloading apps after uninstall failed", exc_info=True)

            self.log.info("uninstalled app %s", app_id)
            self._emit(app_id, "uninstalled", "Removed", name=name, enabled=False)
            return {"id": app_id, "name": name, "installed": False, "data_kept": bool(keep_data)}

    def _forget(self, app_id: str) -> None:
        """Drop the plugin manager's record for a deleted app.

        PluginManager has no removal API -- ``load_all()`` only ever adds -- so
        a stale record would keep a ghost in ``GET /api/apps`` until a restart.
        """
        records = getattr(self.plugins, "_apps", None)
        if isinstance(records, dict):
            records.pop(app_id, None)

    def __repr__(self) -> str:
        return "AppStore(repositories=%d)" % len(self.repositories())


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover
        pass


def _copy_tree(source: Path, dest: Path) -> None:
    """Copy a local directory, refusing to follow symlinks out of it."""
    shutil.copytree(str(source), str(dest), symlinks=True, ignore=shutil.ignore_patterns(".git"))
    root = dest.resolve()
    for current, directories, files in os.walk(str(dest)):
        for name in list(directories) + list(files):
            candidate = Path(current) / name
            if not candidate.is_symlink():
                continue
            resolved = (candidate.parent / os.readlink(str(candidate))).resolve()
            if resolved != root and root not in resolved.parents:
                _rmtree(dest)
                raise ApiError(
                    400,
                    "unsafe_source",
                    "That directory contains a symlink pointing outside itself: %s" % name,
                )


__all__ = [
    "AppStore",
    "CACHE_TTL_SECONDS",
    "DEFAULT_REPOSITORY",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_DOWNLOAD_BYTES",
    "MAX_UNPACKED_BYTES",
    "compare_versions",
    "fetch_bytes",
    "find_app_root",
    "git_clone",
    "parse_version",
    "repository_kind",
    "safe_extract_zip",
    "version_newer",
]
