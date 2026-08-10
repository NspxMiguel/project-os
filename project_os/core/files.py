"""The sandboxed filesystem.

Advanced mode hands a browser a file manager. That makes this module the widest
attack surface in project-os, so it is built around a single rule:

**:func:`resolve` is the only way to turn a client-supplied string into a path,
and every other function in this module starts by calling it.** There is no
second door -- no "internal" helper that takes a raw path, no fast path for
trusted callers.

What ``resolve`` refuses, in order:

* NUL bytes and backslashes (a Windows-style path is never legitimate here);
* percent-encoded traversal -- ``%2e%2e/`` is decoded before the segment checks,
  so a double-encoded ``..`` cannot sneak past a naive string comparison;
* ``..`` segments and empty segments (``a//b``, ``....//....//``);
* absurd lengths (4096 for the whole path, 255 per segment, the kernel's own
  limits);
* anything whose **fully resolved** location falls outside the allowed roots.
  The check happens after :meth:`pathlib.Path.resolve`, so a symlink inside the
  sandbox that points at ``/etc`` is rejected on its target, not on its name.

The roots are ``PROJECT_OS_HOME`` plus whatever ``security.file_roots`` adds.
Writes are additionally gated on ``security.allow_file_write``.

Every function that touches the disk is ``async`` and does its work in the
default executor: a directory listing on a slow SD card must not stall the
event loop that is also streaming the dashboard.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import shutil
import stat as _stat
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import unquote

from project_os import paths
from project_os.errors import ApiError

log = logging.getLogger(__name__)

#: Longest path we will even look at (Linux PATH_MAX).
MAX_PATH_LENGTH = 4096
#: Longest single name (Linux NAME_MAX).
MAX_SEGMENT_LENGTH = 255
#: How many entries a single listing returns before it stops.
MAX_ENTRIES = 5000
#: Default ceiling for :func:`read_text`.
DEFAULT_MAX_READ_BYTES = 1024 * 1024
#: Hard ceiling; a caller asking for more than this gets this.
MAX_READ_BYTES = 8 * 1024 * 1024
#: Bytes sniffed for a NUL before a file is declared binary.
SNIFF_BYTES = 8192
#: Largest body :func:`write_text` accepts.
MAX_WRITE_BYTES = 8 * 1024 * 1024
#: Default upload ceiling; ``security.max_upload_mb`` overrides it.
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
#: Chunk size used when streaming an upload to disk.
UPLOAD_FLUSH_BYTES = 256 * 1024
#: Mode for files this module creates.
NEW_FILE_MODE = 0o644
#: Mode for directories this module creates.
NEW_DIR_MODE = 0o755

_EXTENSION_KINDS = {
    "text": (
        ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".ini",
        ".cfg", ".conf", ".env", ".service", ".timer", ".desktop", ".list",
        ".gitignore", ".editorconfig",
    ),
    "code": (
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh",
        ".bash", ".zsh", ".fish", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs",
        ".go", ".java", ".kt", ".rb", ".php", ".pl", ".lua", ".sql", ".html",
        ".htm", ".css", ".scss", ".less", ".vue", ".svelte", ".patch", ".diff",
    ),
    "config": (".yaml", ".yml", ".json", ".toml", ".xml", ".plist", ".properties"),
    "image": (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
        ".tif", ".tiff", ".heic", ".avif",
    ),
    "audio": (
        ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".oga", ".opus", ".aac",
        ".wma", ".aif", ".aiff", ".mid", ".midi",
    ),
    "video": (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"),
    "archive": (
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".xz", ".txz", ".zst",
        ".7z", ".rar", ".deb", ".rpm", ".img", ".iso", ".whl",
    ),
    "document": (".pdf", ".epub", ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ppt", ".pptx"),
    "binary": (".so", ".o", ".a", ".bin", ".pyc", ".pyo", ".ko", ".dll", ".exe", ".dylib"),
    "database": (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"),
    "font": (".ttf", ".otf", ".woff", ".woff2", ".eot"),
    "key": (".pem", ".key", ".crt", ".cer", ".pub", ".gpg", ".asc"),
}

_KIND_BY_EXTENSION = {}  # type: Dict[str, str]
for _kind, _extensions in _EXTENSION_KINDS.items():
    for _extension in _extensions:
        _KIND_BY_EXTENSION[_extension] = _kind

#: Extension-less names that are still plainly text.
_KIND_BY_NAME = {
    "makefile": "code",
    "dockerfile": "code",
    "vagrantfile": "code",
    "readme": "text",
    "license": "text",
    "licence": "text",
    "changelog": "text",
    "authors": "text",
    "notice": "text",
    "procfile": "config",
}

#: Kinds a text editor can safely open.
EDITABLE_KINDS = frozenset(("text", "code", "config"))


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
def _invalid(message: str, detail: Any = None) -> ApiError:
    return ApiError(400, "invalid_path", message, detail)


def _outside(message: str = "", detail: Any = None) -> ApiError:
    return ApiError(
        403,
        "path_outside_root",
        message or "That path is outside the folders project-os is allowed to touch.",
        detail,
    )


def _no_such(path: Any) -> ApiError:
    return ApiError(404, "not_found", "Não existe esse arquivo ou pasta: %s" % (path,))


def _read_only() -> ApiError:
    return ApiError(
        403,
        "read_only",
        "A escrita de arquivos está desligada. Ligue com security.allow_file_write: true.",
        {"config_key": "security.allow_file_write"},
    )


def _oserror(exc: OSError, path: Any) -> ApiError:
    """Translate the handful of errnos a file manager actually produces."""
    number = getattr(exc, "errno", None)
    if number in (errno.ENOENT, errno.ENOTDIR):
        return _no_such(path)
    if number in (errno.EACCES, errno.EPERM):
        return ApiError(403, "permission_denied", "O sistema recusou: %s" % (path,))
    if number == errno.EISDIR:
        return ApiError(400, "is_a_directory", "%s é uma pasta." % (path,))
    if number == errno.ENOTEMPTY:
        return ApiError(409, "not_empty", "%s não está vazia." % (path,))
    if number == errno.EEXIST:
        return ApiError(409, "already_exists", "%s já existe." % (path,))
    if number == errno.ENOSPC:
        return ApiError(507, "no_space", "O disco está cheio.")
    if number == errno.EXDEV:  # pragma: no cover - handled by shutil.move
        return ApiError(400, "cross_device", "A origem e o destino estão em discos diferentes.")
    if number == errno.ENAMETOOLONG:
        return _invalid("That name is too long.")
    if number == errno.ELOOP:
        return _invalid("Too many symbolic links in %s." % (path,))
    log.warning("filesystem error on %s: %s", path, exc)
    return ApiError(500, "io_error", "O sistema de arquivos deu erro: %s" % (exc,))


# ---------------------------------------------------------------------------
# config / roots
# ---------------------------------------------------------------------------
def _config(config: Any = None) -> Any:
    if config is not None:
        return config
    from project_os.config import get_config  # local import: avoids an import cycle

    return get_config()


def _configured_extra_roots(config: Any) -> List[Path]:
    try:
        raw = config.get("security.file_roots", []) or []
    except Exception:  # pragma: no cover - a broken config must not open the box
        log.warning("security.file_roots is unreadable; falling back to PROJECT_OS_HOME only")
        return []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        log.warning("security.file_roots must be a list, got %r", type(raw).__name__)
        return []
    out = []  # type: List[Path]
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            log.warning("ignoring non-string entry in security.file_roots: %r", item)
            continue
        try:
            candidate = Path(item.strip()).expanduser().resolve()
        except OSError as exc:  # pragma: no cover - unresolvable mount
            log.warning("ignoring unusable file root %r: %s", item, exc)
            continue
        out.append(candidate)
    return out


def root_paths(config: Any = None) -> List[Path]:
    """Every directory the sandbox allows, home first, de-duplicated."""
    cfg = _config(config)
    try:
        first = paths.home()
    except OSError as exc:  # pragma: no cover - unwritable home
        raise ApiError(500, "no_home", "O PROJECT_OS_HOME não serve: %s" % exc)
    out = [first]
    seen = {str(first)}
    for candidate in _configured_extra_roots(cfg):
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _root_id(path: Path, is_home: bool) -> str:
    if is_home:
        return "home"
    digest = hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:8]
    return "r-%s" % digest


def roots(config: Any = None) -> List[Dict[str, Any]]:
    """Root descriptors for the UI's folder picker.

    Each entry is ``{id, name, path, is_home, exists, writable}``. ``writable``
    combines ``security.allow_file_write`` with the actual filesystem
    permission, so a read-only USB stick shows up as read-only.
    """
    cfg = _config(config)
    allowed = write_allowed(cfg)
    out = []  # type: List[Dict[str, Any]]
    for index, path in enumerate(root_paths(cfg)):
        is_home = index == 0
        exists = path.is_dir()
        out.append(
            {
                "id": _root_id(path, is_home),
                "name": "project-os" if is_home else (path.name or str(path)),
                "path": str(path),
                "is_home": is_home,
                "exists": exists,
                "writable": bool(allowed and exists and os.access(str(path), os.W_OK)),
            }
        )
    return out


def write_allowed(config: Any = None) -> bool:
    try:
        return bool(_config(config).get("security.allow_file_write", True))
    except Exception:  # pragma: no cover
        return False


def require_write(config: Any = None) -> None:
    if not write_allowed(config):
        raise _read_only()


# ---------------------------------------------------------------------------
# resolve -- the chokepoint
# ---------------------------------------------------------------------------
def _decode_repeatedly(value: str, rounds: int = 3) -> str:
    """Percent-decode until it stops changing (bounded).

    ``%252e%252e`` decodes to ``%2e%2e`` and then to ``..``; one pass would
    have missed it.
    """
    current = value
    for _ in range(rounds):
        try:
            decoded = unquote(current)
        except Exception:  # pragma: no cover - unquote is total in practice
            return current
        if decoded == current:
            return current
        current = decoded
    return current


def _reject_dangerous(raw: str, original: str) -> None:
    """Segment-level checks, run against both the raw and the decoded form."""
    if "\x00" in raw:
        raise _invalid("A path cannot contain a NUL byte.", {"path": original[:120]})
    if "\\" in raw:
        raise _invalid(
            "Backslashes are not path separators here; use '/'.", {"path": original[:120]}
        )
    if len(raw) > MAX_PATH_LENGTH:
        raise _invalid("That path is too long (limit %d characters)." % MAX_PATH_LENGTH)
    body = raw[1:] if raw.startswith("/") else raw
    if body.endswith("/"):
        body = body[:-1]
    if not body:
        return
    for segment in body.split("/"):
        if segment == "":
            raise _invalid("That path has an empty segment.", {"path": original[:120]})
        if segment == "..":
            raise _outside("'..' is not allowed in a path.", {"path": original[:120]})
        if len(segment) > MAX_SEGMENT_LENGTH:
            raise _invalid("'%s...' is too long for a file name." % segment[:32])


def _contained(candidate: Path, roots_: Iterable[Path]) -> bool:
    """Purely lexical containment -- both sides are already fully resolved."""
    for root in roots_:
        if candidate == root:
            return True
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def resolve(path: Any = "", config: Any = None, follow_final: bool = True) -> Path:
    """Turn a client-supplied path into a real, in-sandbox :class:`Path`.

    Relative paths are relative to ``PROJECT_OS_HOME``; absolute paths are
    accepted only when they land inside one of :func:`root_paths`.

    ``follow_final=False`` resolves the *parent* fully but leaves the last
    component alone, which is what delete and rename need: they must act on a
    symlink itself, not on whatever it points at.

    Raises :class:`~project_os.errors.ApiError` -- ``invalid_path`` (400) or
    ``path_outside_root`` (403) -- and never returns a path outside the roots.
    """
    original = "" if path is None else str(path)
    raw = original.strip()
    _reject_dangerous(raw, original)
    decoded = _decode_repeatedly(raw)
    if decoded != raw:
        _reject_dangerous(decoded, original)

    allowed = root_paths(config)
    home_root = allowed[0]

    if raw in ("", ".", "./"):
        return home_root

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = home_root / candidate

    try:
        if follow_final:
            final = candidate.resolve()
            checked = final
        else:
            name = candidate.name
            if not name:  # a bare "/" or a root itself
                final = candidate.resolve()
                checked = final
            else:
                parent = candidate.parent.resolve()
                final = parent / name
                checked = parent
    except OSError as exc:  # pragma: no cover - ELOOP and friends
        raise _oserror(exc, original)
    except RuntimeError:  # pragma: no cover - symlink loop on some platforms
        raise _invalid("Too many symbolic links in that path.")

    if not _contained(checked, allowed):
        raise _outside(detail={"path": original[:200]})
    return final


def relative_to_root(path: Path, config: Any = None) -> Tuple[Optional[Path], str]:
    """``(root, path-relative-to-it)`` for display. Falsy when not contained."""
    for root in root_paths(config):
        if path == root:
            return root, ""
        try:
            return root, str(path.relative_to(root))
        except ValueError:
            continue
    return None, str(path)


def is_root(path: Path, config: Any = None) -> bool:
    return any(path == root for root in root_paths(config))


def parent_of(path: Path, config: Any = None) -> Optional[Path]:
    """The parent, or ``None`` when ``path`` is a root (nothing above it)."""
    if is_root(path, config):
        return None
    parent = path.parent
    if parent == path:
        return None
    if not _contained(parent, root_paths(config)):
        return None
    return parent


# ---------------------------------------------------------------------------
# entry description
# ---------------------------------------------------------------------------
def kind_for(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "dir"
    lowered = name.lower()
    named = _KIND_BY_NAME.get(lowered)
    if named:
        return named
    suffix = os.path.splitext(lowered)[1]
    if suffix:
        found = _KIND_BY_EXTENSION.get(suffix)
        if found:
            return found
    if lowered.startswith("."):
        return "text"
    return "file"


def _mode_string(st_mode: int) -> str:
    return "%04o" % _stat.S_IMODE(st_mode)


def _describe(
    path: Path,
    root: Optional[Path],
    lst: Optional[os.stat_result] = None,
    config: Any = None,
    allowed: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """One entry, tolerant of a file that vanished or refuses to be stat()ed."""
    name = path.name or str(path)
    entry = {
        "name": name,
        "path": str(path),
        "relative": str(path.relative_to(root)) if root is not None and path != root else "",
        "is_dir": False,
        "is_file": False,
        "is_symlink": False,
        "target": None,
        "escapes": False,
        "size": None,
        "mtime": None,
        "mode": None,
        "kind": "file",
        "hidden": name.startswith("."),
        "readable": False,
        "editable": False,
    }  # type: Dict[str, Any]

    try:
        link_stat = lst if lst is not None else os.lstat(str(path))
    except OSError:
        entry["kind"] = "unknown"
        return entry

    entry["is_symlink"] = _stat.S_ISLNK(link_stat.st_mode)
    target_stat = link_stat
    if entry["is_symlink"]:
        try:
            entry["target"] = os.readlink(str(path))
        except OSError:
            entry["target"] = None
        try:
            target_stat = os.stat(str(path))
        except OSError:
            entry["kind"] = "broken_link"
            entry["mode"] = _mode_string(link_stat.st_mode)
            entry["mtime"] = round(link_stat.st_mtime, 3)
            return entry
        roots_ = allowed if allowed is not None else root_paths(config)
        try:
            entry["escapes"] = not _contained(Path(os.path.realpath(str(path))), roots_)
        except OSError:  # pragma: no cover
            entry["escapes"] = True

    entry["is_dir"] = _stat.S_ISDIR(target_stat.st_mode)
    entry["is_file"] = _stat.S_ISREG(target_stat.st_mode)
    entry["size"] = int(target_stat.st_size) if entry["is_file"] else None
    entry["mtime"] = round(target_stat.st_mtime, 3)
    entry["mode"] = _mode_string(target_stat.st_mode)
    if entry["is_dir"]:
        entry["kind"] = "dir"
    elif entry["is_file"]:
        entry["kind"] = kind_for(name, False)
    else:
        entry["kind"] = "special"
    entry["readable"] = os.access(str(path), os.R_OK)
    entry["editable"] = bool(
        entry["is_file"]
        and entry["readable"]
        and not entry["escapes"]
        and entry["kind"] in EDITABLE_KINDS
        and (entry["size"] or 0) <= DEFAULT_MAX_READ_BYTES
    )
    return entry


# ---------------------------------------------------------------------------
# executor plumbing
# ---------------------------------------------------------------------------
async def _run(func: Any, *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def _require_dir(path: Path) -> os.stat_result:
    try:
        st = os.stat(str(path))
    except OSError as exc:
        raise _oserror(exc, path)
    if not _stat.S_ISDIR(st.st_mode):
        raise ApiError(400, "not_a_directory", "%s não é uma pasta." % path)
    return st


def _require_regular_file(path: Path) -> os.stat_result:
    try:
        st = os.stat(str(path))
    except OSError as exc:
        raise _oserror(exc, path)
    if _stat.S_ISDIR(st.st_mode):
        raise ApiError(400, "is_a_directory", "%s é uma pasta." % path)
    if not _stat.S_ISREG(st.st_mode):
        # A FIFO or device node would block the executor thread forever.
        raise ApiError(400, "not_a_file", "%s não é um arquivo comum." % path)
    return st


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def _list_dir_sync(path: Any, config: Any, include_hidden: bool) -> List[Dict[str, Any]]:
    target = resolve(path, config)
    _require_dir(target)
    allowed = root_paths(config)
    root, _ = relative_to_root(target, config)
    entries = []  # type: List[Dict[str, Any]]
    try:
        with os.scandir(str(target)) as scanner:
            for item in scanner:
                if len(entries) >= MAX_ENTRIES:
                    log.warning("listing of %s truncated at %d entries", target, MAX_ENTRIES)
                    break
                if not include_hidden and item.name.startswith("."):
                    continue
                try:
                    link_stat = item.stat(follow_symlinks=False)
                except OSError:
                    link_stat = None
                entries.append(
                    _describe(target / item.name, root, link_stat, config, allowed)
                )
    except OSError as exc:
        raise _oserror(exc, target)
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower(), e["name"]))
    return entries


async def list_dir(
    path: Any = "", config: Any = None, include_hidden: bool = True
) -> List[Dict[str, Any]]:
    """Entries of a directory, directories first then case-insensitive by name.

    Stops at :data:`MAX_ENTRIES`; a caller that gets exactly that many should
    tell the user the listing was cut short.
    """
    return await _run(_list_dir_sync, path, config, include_hidden)


def _stat_sync(path: Any, config: Any) -> Dict[str, Any]:
    target = resolve(path, config)
    root, relative = relative_to_root(target, config)
    info = _describe(target, root, None, config)
    parent = parent_of(target, config)
    info["parent"] = str(parent) if parent is not None else None
    info["root"] = str(root) if root is not None else None
    info["relative"] = relative
    info["is_root"] = is_root(target, config)
    info["exists"] = os.path.lexists(str(target))
    return info


async def stat(path: Any, config: Any = None) -> Dict[str, Any]:
    """Describe one path, with its parent and root for navigation."""
    return await _run(_stat_sync, path, config)


def _read_text_sync(path: Any, max_bytes: Optional[int], config: Any) -> Dict[str, Any]:
    target = resolve(path, config)
    st = _require_regular_file(target)
    limit = DEFAULT_MAX_READ_BYTES if max_bytes is None else int(max_bytes)
    limit = max(1, min(limit, MAX_READ_BYTES))
    size = int(st.st_size)
    if size > limit:
        raise ApiError(
            413,
            "too_large",
            "%s tem %d bytes; o editor só abre arquivos até %d." % (target.name, size, limit),
            {"size": size, "limit": limit},
        )
    try:
        with open(str(target), "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise _oserror(exc, target)
    if len(data) > limit:  # grew between stat() and read()
        raise ApiError(
            413,
            "too_large",
            "%s passou do limite de %d bytes enquanto estava sendo lido." % (target.name, limit),
            {"limit": limit},
        )
    if b"\x00" in data[:SNIFF_BYTES]:
        raise ApiError(
            400,
            "binary_file",
            "%s looks like a binary file; download it instead of opening it." % target.name,
            {"path": str(target)},
        )
    lossy = False
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = data.decode("utf-8", "replace")
        lossy = True
    root, relative = relative_to_root(target, config)
    return {
        "path": str(target),
        "name": target.name,
        "relative": relative,
        "root": str(root) if root is not None else None,
        "content": content,
        "size": len(data),
        "encoding": "utf-8",
        "lossy": lossy,
        "mode": _mode_string(st.st_mode),
        "mtime": round(st.st_mtime, 3),
        "kind": kind_for(target.name, False),
    }


async def read_text(
    path: Any, max_bytes: Optional[int] = None, config: Any = None
) -> Dict[str, Any]:
    """Read a text file.

    Refuses directories, non-regular files, anything with a NUL in its first
    8 KB (``binary_file``) and anything over the limit (``too_large``).
    ``lossy`` is True when invalid UTF-8 had to be replaced -- saving such a
    file back would corrupt it, and the UI should say so.
    """
    return await _run(_read_text_sync, path, max_bytes, config)


def _read_bytes_sync(path: Any, max_bytes: Optional[int], config: Any) -> bytes:
    target = resolve(path, config)
    st = _require_regular_file(target)
    limit = MAX_READ_BYTES if max_bytes is None else max(1, int(max_bytes))
    if st.st_size > limit:
        raise ApiError(
            413,
            "too_large",
            "%s é maior que %d bytes." % (target.name, limit),
            {"size": int(st.st_size), "limit": limit},
        )
    try:
        with open(str(target), "rb") as handle:
            return handle.read(limit)
    except OSError as exc:
        raise _oserror(exc, target)


async def read_bytes(
    path: Any, max_bytes: Optional[int] = None, config: Any = None
) -> bytes:
    """Raw bytes, for callers that already know what they are holding."""
    return await _run(_read_bytes_sync, path, max_bytes, config)


def download_path(path: Any, config: Any = None) -> Path:
    """Validate a path for streaming and return it.

    Synchronous on purpose: ``FileResponse`` wants a path, and it does its own
    reading in a worker thread.
    """
    target = resolve(path, config)
    _require_regular_file(target)
    return target


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
def _atomic_write(target: Path, data: bytes, mode: Optional[int]) -> os.stat_result:
    """Temp file in the same directory, fsync, then ``os.replace``.

    Same directory matters: ``os.replace`` is only atomic within a filesystem,
    and ``/tmp`` is frequently a different one.
    """
    parent = target.parent
    handle = None
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(parent), prefix="." + target.name[:64] + ".", suffix=".tmp"
        )
        handle = os.fdopen(fd, "wb")
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(tmp_name, NEW_FILE_MODE if mode is None else mode)
        os.replace(tmp_name, str(target))
        tmp_name = ""
    except OSError as exc:
        raise _oserror(exc, target)
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover
                pass
    try:
        dir_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:  # pragma: no cover - not every filesystem allows this
        pass
    return os.stat(str(target))


def _write_text_sync(
    path: Any, content: Any, create_dirs: bool, config: Any
) -> Dict[str, Any]:
    require_write(config)
    target = resolve(path, config)
    if is_root(target, config):
        raise ApiError(400, "is_a_directory", "Isso é uma pasta raiz, não um arquivo.")
    text = "" if content is None else str(content)
    data = text.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ApiError(
            413,
            "too_large",
            "Esse arquivo tem %d bytes; o limite é %d." % (len(data), MAX_WRITE_BYTES),
            {"limit": MAX_WRITE_BYTES},
        )
    existing_mode = None  # type: Optional[int]
    try:
        current = os.stat(str(target))
    except OSError as exc:
        if getattr(exc, "errno", None) not in (errno.ENOENT, errno.ENOTDIR):
            raise _oserror(exc, target)
    else:
        if _stat.S_ISDIR(current.st_mode):
            raise ApiError(400, "is_a_directory", "%s é uma pasta." % target)
        if not _stat.S_ISREG(current.st_mode):
            raise ApiError(400, "not_a_file", "%s não é um arquivo comum." % target)
        existing_mode = _stat.S_IMODE(current.st_mode)

    parent = target.parent
    if not parent.is_dir():
        if not create_dirs:
            raise _no_such(parent)
        try:
            os.makedirs(str(parent), mode=NEW_DIR_MODE, exist_ok=True)
        except OSError as exc:
            raise _oserror(exc, parent)

    st = _atomic_write(target, data, existing_mode)
    root, relative = relative_to_root(target, config)
    return {
        "path": str(target),
        "name": target.name,
        "relative": relative,
        "root": str(root) if root is not None else None,
        "size": int(st.st_size),
        "mode": _mode_string(st.st_mode),
        "mtime": round(st.st_mtime, 3),
        "created": existing_mode is None,
    }


async def write_text(
    path: Any, content: Any, create_dirs: bool = False, config: Any = None
) -> Dict[str, Any]:
    """Write a text file atomically, preserving the mode of an existing file.

    Gated on ``security.allow_file_write``.
    """
    return await _run(_write_text_sync, path, content, create_dirs, config)


def _mkdir_sync(path: Any, parents: bool, exist_ok: bool, config: Any) -> Dict[str, Any]:
    require_write(config)
    target = resolve(path, config)
    if is_root(target, config):
        if exist_ok:
            return _stat_sync(str(target), config)
        raise ApiError(409, "already_exists", "%s já existe." % target)
    try:
        if parents:
            os.makedirs(str(target), mode=NEW_DIR_MODE, exist_ok=exist_ok)
        else:
            os.mkdir(str(target), NEW_DIR_MODE)
    except FileExistsError:
        if exist_ok:
            return _stat_sync(str(target), config)
        raise ApiError(409, "already_exists", "%s já existe." % target)
    except OSError as exc:
        raise _oserror(exc, target)
    return _stat_sync(str(target), config)


async def mkdir(
    path: Any, parents: bool = False, exist_ok: bool = False, config: Any = None
) -> Dict[str, Any]:
    """Create a directory. Gated on ``security.allow_file_write``."""
    return await _run(_mkdir_sync, path, parents, exist_ok, config)


def _delete_sync(path: Any, recursive: bool, config: Any) -> Dict[str, Any]:
    require_write(config)
    # follow_final=False: deleting a symlink must remove the link, not its target.
    target = resolve(path, config, False)
    if is_root(target, config):
        raise ApiError(
            400, "protected_path", "%s é uma pasta raiz e não pode ser apagada." % target
        )
    try:
        link_stat = os.lstat(str(target))
    except OSError as exc:
        raise _oserror(exc, target)

    kind = "file"
    if _stat.S_ISLNK(link_stat.st_mode):
        kind = "symlink"
        try:
            os.unlink(str(target))
        except OSError as exc:
            raise _oserror(exc, target)
    elif _stat.S_ISDIR(link_stat.st_mode):
        kind = "dir"
        if not recursive:
            raise ApiError(
                400,
                "is_a_directory",
                "%s é uma pasta. Apague com recursive: true se for isso mesmo." % target.name,
                {"path": str(target)},
            )
        errors = []  # type: List[str]

        def _on_error(func: Any, name: str, exc_info: Any) -> None:
            errors.append(str(name))

        shutil.rmtree(str(target), onerror=_on_error)
        if errors:
            raise ApiError(
                500,
                "delete_failed",
                "%d item(ns) dentro de %s não puderam ser apagados." % (len(errors), target.name),
                {"failed": errors[:20]},
            )
    else:
        try:
            os.unlink(str(target))
        except OSError as exc:
            raise _oserror(exc, target)

    parent = parent_of(target, config)
    return {
        "path": str(target),
        "name": target.name,
        "kind": kind,
        "deleted": True,
        "parent": str(parent) if parent is not None else None,
    }


async def delete(path: Any, recursive: bool = False, config: Any = None) -> Dict[str, Any]:
    """Delete a file, a symlink, or -- only with ``recursive`` -- a directory."""
    return await _run(_delete_sync, path, recursive, config)


def _destination(source: Path, raw_dest: Any, config: Any) -> Path:
    """Resolve a destination, with ``mv``-style "into an existing folder"."""
    dest = resolve(raw_dest, config, False)
    if dest.is_dir() and not dest.is_symlink():
        dest = resolve(str(dest / source.name), config, False)
    return dest


def _prepare_move_target(source: Path, dest: Path, overwrite: bool, config: Any) -> None:
    if is_root(source, config):
        raise ApiError(400, "protected_path", "%s é uma pasta raiz." % source)
    if is_root(dest, config):
        raise ApiError(409, "already_exists", "%s é uma pasta raiz." % dest)
    if source == dest:
        raise ApiError(400, "same_path", "A origem e o destino são o mesmo arquivo.")
    if not os.path.lexists(str(source)):
        raise _no_such(source)
    if not dest.parent.is_dir():
        raise _no_such(dest.parent)
    if os.path.lexists(str(dest)):
        if not overwrite:
            raise ApiError(
                409,
                "already_exists",
                "%s já existe. Mande overwrite: true para substituir." % dest.name,
                {"path": str(dest)},
            )
        try:
            dest_stat = os.lstat(str(dest))
        except OSError as exc:
            raise _oserror(exc, dest)
        if _stat.S_ISDIR(dest_stat.st_mode) and not _stat.S_ISLNK(dest_stat.st_mode):
            raise ApiError(
                409,
                "already_exists",
                "%s é uma pasta que já existe; apague ela antes." % dest.name,
            )
        try:
            os.unlink(str(dest))
        except OSError as exc:
            raise _oserror(exc, dest)


def _rename_sync(src: Any, dst: Any, overwrite: bool, config: Any) -> Dict[str, Any]:
    require_write(config)
    source = resolve(src, config, False)
    dest = _destination(source, dst, config)
    if str(dest).startswith(str(source) + os.sep):
        raise ApiError(400, "invalid_move", "Uma pasta não pode ser movida para dentro dela mesma.")
    _prepare_move_target(source, dest, overwrite, config)
    try:
        # shutil.move, not os.replace: a media root can live on another disk.
        shutil.move(str(source), str(dest))
    except OSError as exc:
        raise _oserror(exc, dest)
    except shutil.Error as exc:
        raise ApiError(400, "move_failed", str(exc))
    return _stat_sync(str(dest), config)


async def rename(
    src: Any, dst: Any, overwrite: bool = False, config: Any = None
) -> Dict[str, Any]:
    """Move or rename. Both ends go through :func:`resolve`.

    When ``dst`` is an existing directory the item moves *into* it, the way
    ``mv`` behaves and the way a file manager's drag-and-drop expects.
    """
    return await _run(_rename_sync, src, dst, overwrite, config)


#: ``move`` reads better than ``rename`` at some call sites.
move = rename


def _copy_sync(src: Any, dst: Any, overwrite: bool, config: Any) -> Dict[str, Any]:
    require_write(config)
    # follow_final=True on the source: copying through a symlink that escapes
    # the sandbox would smuggle outside content in.
    source = resolve(src, config)
    dest = _destination(source, dst, config)
    if str(dest).startswith(str(source) + os.sep):
        raise ApiError(400, "invalid_copy", "Uma pasta não pode ser copiada para dentro dela mesma.")
    _prepare_move_target(source, dest, overwrite, config)
    try:
        source_stat = os.stat(str(source))
    except OSError as exc:
        raise _oserror(exc, source)
    try:
        if _stat.S_ISDIR(source_stat.st_mode):
            shutil.copytree(str(source), str(dest), symlinks=True)
        elif _stat.S_ISREG(source_stat.st_mode):
            shutil.copy2(str(source), str(dest))
        else:
            raise ApiError(400, "not_a_file", "%s não é um arquivo comum." % source)
    except OSError as exc:
        raise _oserror(exc, dest)
    except shutil.Error as exc:
        raise ApiError(500, "copy_failed", str(exc))
    return _stat_sync(str(dest), config)


async def copy(
    src: Any, dst: Any, overwrite: bool = False, config: Any = None
) -> Dict[str, Any]:
    """Copy a file or a whole directory tree."""
    return await _run(_copy_sync, src, dst, overwrite, config)


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------
_UNSAFE_NAME_CHARS = set('/\\\x00:*?"<>|')


def sanitize_filename(filename: Any, fallback: str = "upload") -> str:
    """Reduce a client-supplied name to one safe path segment.

    Directory components are dropped rather than escaped: an upload named
    ``../../etc/cron.d/evil`` becomes ``evil``, and even that only lands where
    the caller already proved it may write.
    """
    raw = "" if filename is None else str(filename)
    raw = raw.replace("\\", "/").split("/")[-1]
    cleaned = "".join(
        ch for ch in raw if ch not in _UNSAFE_NAME_CHARS and (ch >= " " or ch == "\t")
    )
    cleaned = cleaned.replace("\t", " ").strip().strip(".")
    cleaned = " ".join(cleaned.split())
    if not cleaned or cleaned in (".", ".."):
        return fallback
    if len(cleaned) > MAX_SEGMENT_LENGTH:
        stem, extension = os.path.splitext(cleaned)
        extension = extension[:16]
        cleaned = stem[: MAX_SEGMENT_LENGTH - len(extension)] + extension
    return cleaned


def max_upload_bytes(config: Any = None) -> int:
    try:
        megabytes = _config(config).get("security.max_upload_mb", None)
    except Exception:  # pragma: no cover
        megabytes = None
    if megabytes is None:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(float(megabytes) * 1024 * 1024)
    except (TypeError, ValueError):
        log.warning("security.max_upload_mb is not a number: %r", megabytes)
        return DEFAULT_MAX_UPLOAD_BYTES
    return max(1, value)


def _open_upload_temp(dest_dir: Path, name: str) -> Tuple[Any, str]:
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest_dir), prefix=".upload-" + name[:48] + ".", suffix=".part"
    )
    return os.fdopen(fd, "wb"), tmp_name


async def _iter_chunks(stream: Any) -> AsyncIterator[bytes]:
    """Accept the several shapes an upload body arrives in."""
    if stream is None:
        return
    if isinstance(stream, (bytes, bytearray, memoryview)):
        yield bytes(stream)
        return
    if hasattr(stream, "__aiter__"):
        async for chunk in stream:
            if chunk:
                yield bytes(chunk)
        return
    read = getattr(stream, "read", None)
    if callable(read):
        while True:
            chunk = read(UPLOAD_FLUSH_BYTES)
            if asyncio.iscoroutine(chunk):
                chunk = await chunk
            if not chunk:
                return
            yield bytes(chunk)
        return
    if hasattr(stream, "__iter__"):
        for chunk in stream:
            if chunk:
                yield bytes(chunk)
        return
    raise ApiError(400, "bad_upload", "Não deu para ler o envio.")


async def save_upload(
    dest_dir: Any,
    filename: Any,
    stream: Any,
    overwrite: bool = False,
    max_bytes: Optional[int] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Stream an upload into ``dest_dir`` under a sanitized name.

    Written to a hidden ``.part`` file in the destination directory and moved
    into place only once the whole body has arrived, so an interrupted upload
    never leaves a plausible-looking truncated file behind.
    """
    require_write(config)
    directory = await _run(_resolve_dir_sync, dest_dir, config)
    name = sanitize_filename(filename)
    target = resolve(str(directory / name), config, False)
    if target.parent != directory:  # pragma: no cover - sanitize_filename prevents this
        raise _outside()
    if os.path.lexists(str(target)) and not overwrite:
        raise ApiError(
            409,
            "already_exists",
            "%s já existe aqui. Mande overwrite=true para substituir." % name,
            {"path": str(target), "name": name},
        )

    limit = max_upload_bytes(config) if max_bytes is None else max(1, int(max_bytes))
    handle, tmp_name = await _run(_open_upload_temp, directory, name)
    written = 0
    buffer = bytearray()
    try:
        async for chunk in _iter_chunks(stream):
            written += len(chunk)
            if written > limit:
                raise ApiError(
                    413,
                    "too_large",
                    "Esse envio passa do limite de %d bytes." % limit,
                    {"limit": limit},
                )
            buffer.extend(chunk)
            if len(buffer) >= UPLOAD_FLUSH_BYTES:
                payload = bytes(buffer)
                del buffer[:]
                await _run(handle.write, payload)
        if buffer:
            await _run(handle.write, bytes(buffer))
        await _run(handle.flush)
        await _run(os.fsync, handle.fileno())
    except BaseException:
        await _run(_discard_temp, handle, tmp_name)
        raise
    await _run(handle.close)

    try:
        await _run(os.chmod, tmp_name, NEW_FILE_MODE)
        await _run(os.replace, tmp_name, str(target))
    except OSError as exc:
        await _run(_discard_temp, None, tmp_name)
        raise _oserror(exc, target)
    info = await _run(_stat_sync, str(target), config)
    info["uploaded_bytes"] = written
    return info


def _resolve_dir_sync(path: Any, config: Any) -> Path:
    directory = resolve(path, config)
    _require_dir(directory)
    return directory


def _discard_temp(handle: Any, tmp_name: str) -> None:
    if handle is not None:
        try:
            handle.close()
        except OSError:  # pragma: no cover
            pass
    if tmp_name:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover
            pass


__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "EDITABLE_KINDS",
    "MAX_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_WRITE_BYTES",
    "copy",
    "delete",
    "download_path",
    "is_root",
    "kind_for",
    "list_dir",
    "max_upload_bytes",
    "mkdir",
    "move",
    "parent_of",
    "read_bytes",
    "read_text",
    "relative_to_root",
    "rename",
    "require_write",
    "resolve",
    "root_paths",
    "roots",
    "sanitize_filename",
    "save_upload",
    "stat",
    "write_allowed",
    "write_text",
]
