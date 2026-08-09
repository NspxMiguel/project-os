"""The file manager.

Everything in this module hangs off one function, :func:`_resolve`. A file
manager reachable from a browser is a remote shell if it can be talked out of
its sandbox, so the rule is absolute and stated once:

    every path from the client is resolved with symlinks followed, and then
    checked to be inside a configured root -- **after** resolution, never
    before.

Checking before resolution is the classic hole: ``roots/ok/../../etc/shadow``
and a symlink pointing at ``/`` both pass a string check and both fail this one.

Roots default to the ProjectOS home and the media directory. Widening them is a
``security.file_roots`` decision made in the config file on the machine, not
from the browser -- see :mod:`projectos.api.settings`.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from projectos import auth, paths
from projectos.errors import ApiError
from projectos.main import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

#: Refuse to read a text file bigger than this into the editor. Above it, the
#: browser tab is the thing that dies, and it dies confusingly.
MAX_EDIT_BYTES = 2 * 1024 * 1024

#: Chunk size for uploads. Small enough that a 700 MB Pi does not swap on a big
#: media file, big enough that the loop is not the bottleneck.
UPLOAD_CHUNK = 512 * 1024

TEXT_SUFFIXES = frozenset(
    [".txt", ".md", ".yaml", ".yml", ".json", ".ini", ".conf", ".cfg", ".toml",
     ".py", ".js", ".css", ".html", ".sh", ".service", ".env", ".log", ".csv", ".xml"]
)


class WriteBody(BaseModel):
    content: str = ""


class PathBody(BaseModel):
    path: str = Field(..., min_length=1)


class MoveBody(BaseModel):
    source: str = Field(..., min_length=1)
    destination: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# the sandbox
# ---------------------------------------------------------------------------
def _roots(config: Any) -> List[Path]:
    found = []  # type: List[Path]
    for candidate in [paths.home(), paths.media_dir()]:
        try:
            found.append(candidate.resolve())
        except OSError:  # pragma: no cover - a root that vanished
            continue
    for extra in config.get("security.file_roots", []) or []:
        try:
            resolved = Path(str(extra)).expanduser().resolve()
        except (OSError, RuntimeError):
            log.warning("ignoring unusable file root %r", extra)
            continue
        if resolved.is_dir() and resolved not in found:
            found.append(resolved)
    return found


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve(config: Any, raw: str, must_exist: bool = True) -> Path:
    """Turn a client-supplied path into a real one inside a root, or raise."""
    if not raw or raw in (".", "./"):
        raise ApiError(400, "bad_path", "A path is required.")
    if "\x00" in raw:
        raise ApiError(400, "bad_path", "That path contains a null byte.")
    roots = _roots(config)
    if not roots:  # pragma: no cover - home() creates itself
        raise ApiError(503, "no_roots", "No readable directory is configured.")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        # strict=False so a not-yet-existing file still gets its ".." collapsed
        # and its parent symlinks followed.
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ApiError(400, "bad_path", "That path cannot be resolved.")

    if not any(_within(resolved, root) for root in roots):
        # The message names the roots on purpose: refusing without saying what is
        # allowed makes the file manager feel broken rather than guarded.
        raise ApiError(
            403,
            "outside_sandbox",
            "%s is outside the allowed directories (%s)."
            % (resolved, ", ".join(str(r) for r in roots)),
        )
    if must_exist and not resolved.exists():
        raise ApiError(404, "not_found", "%s does not exist." % resolved)
    return resolved


def _require_write(config: Any) -> None:
    if not config.get("security.allow_file_write", True):
        raise ApiError(403, "read_only", "File writing is turned off in the settings.")


def _entry(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        # A broken symlink or a file removed mid-listing. Show it as unreadable
        # instead of failing the whole directory.
        return {"name": path.name, "path": str(path), "kind": "unknown", "readable": False}
    is_dir = path.is_dir()
    return {
        "name": path.name,
        "path": str(path),
        "kind": "directory" if is_dir else "file",
        "size": None if is_dir else stat.st_size,
        "modified": stat.st_mtime,
        "mode": stat.st_mode & 0o777,
        "readable": os.access(str(path), os.R_OK),
        "writable": os.access(str(path), os.W_OK),
        "editable": (not is_dir) and _looks_textual(path, stat.st_size),
    }


def _looks_textual(path: Path, size: int) -> bool:
    if size > MAX_EDIT_BYTES:
        return False
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    guess = mimetypes.guess_type(path.name)[0] or ""
    return guess.startswith("text/")


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/roots")
async def list_roots(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    return {
        "roots": [{"path": str(root), "name": root.name or str(root)} for root in _roots(config)],
        "writable": bool(config.get("security.allow_file_write", True)),
    }


@router.get("")
@router.get("/list", include_in_schema=False)
async def list_directory(
    path: Optional[str] = Query(None),
    show_hidden: bool = Query(False),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    roots = _roots(config)
    target = _resolve(config, path) if path else roots[0]
    if not target.is_dir():
        raise ApiError(400, "not_a_directory", "%s is a file." % target)
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        raise ApiError(403, "permission_denied", "Cannot read %s." % target)
    entries = [
        _entry(child)
        for child in children
        if show_hidden or not child.name.startswith(".")
    ]
    parent = target.parent
    return {
        "path": str(target),
        "parent": str(parent) if any(_within(parent, r) for r in roots) else None,
        "entries": entries,
        "writable": os.access(str(target), os.W_OK)
        and bool(config.get("security.allow_file_write", True)),
    }


@router.get("/read", response_class=PlainTextResponse)
async def read_file(
    path: str = Query(...),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> str:
    target = _resolve(config, path)
    if target.is_dir():
        raise ApiError(400, "is_a_directory", "%s is a directory." % target)
    if target.stat().st_size > MAX_EDIT_BYTES:
        raise ApiError(
            413,
            "too_large",
            "%s is larger than %d MB; download it instead."
            % (target.name, MAX_EDIT_BYTES // (1024 * 1024)),
        )
    try:
        return target.read_text("utf-8")
    except UnicodeDecodeError:
        raise ApiError(415, "not_text", "%s is not a text file." % target.name)
    except PermissionError:
        raise ApiError(403, "permission_denied", "Cannot read %s." % target)


@router.get("/download")
async def download(
    path: str = Query(...),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> FileResponse:
    target = _resolve(config, path)
    if target.is_dir():
        raise ApiError(400, "is_a_directory", "Cannot download a directory.")
    return FileResponse(str(target), filename=target.name)


@router.put("/write")
async def write_file(
    body: WriteBody,
    path: str = Query(...),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Save a text file.

    Writes to a sibling temp file and renames over the target, so a full disk or
    a crash halfway leaves the previous version intact rather than an empty file
    where the config used to be.
    """
    _require_write(config)
    target = _resolve(config, path, must_exist=False)
    if target.is_dir():
        raise ApiError(400, "is_a_directory", "%s is a directory." % target)
    if not target.parent.is_dir():
        raise ApiError(404, "no_parent", "%s does not exist." % target.parent)
    temporary = target.with_name(target.name + ".projectos-tmp")
    try:
        temporary.write_text(body.content, "utf-8")
        os.replace(str(temporary), str(target))
    except PermissionError:
        raise ApiError(403, "permission_denied", "Cannot write %s." % target)
    except OSError as exc:
        raise ApiError(500, "write_failed", "Could not write %s: %s" % (target, exc))
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return {"ok": True, "path": str(target), "bytes": len(body.content.encode("utf-8"))}


@router.post("/mkdir")
async def make_directory(
    body: PathBody,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    _require_write(config)
    target = _resolve(config, body.path, must_exist=False)
    if target.exists():
        raise ApiError(409, "already_exists", "%s already exists." % target)
    try:
        target.mkdir(parents=True)
    except PermissionError:
        raise ApiError(403, "permission_denied", "Cannot create %s." % target)
    except OSError as exc:
        raise ApiError(500, "mkdir_failed", str(exc))
    return {"ok": True, "path": str(target)}


@router.post("/move")
async def move(
    body: MoveBody,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Move or rename. Both ends are checked against the sandbox separately."""
    _require_write(config)
    source = _resolve(config, body.source)
    destination = _resolve(config, body.destination, must_exist=False)
    if destination.exists():
        raise ApiError(409, "already_exists", "%s already exists." % destination)
    try:
        shutil.move(str(source), str(destination))
    except PermissionError:
        raise ApiError(403, "permission_denied", "Cannot move %s." % source)
    except OSError as exc:
        raise ApiError(500, "move_failed", str(exc))
    return {"ok": True, "source": str(source), "destination": str(destination)}


@router.post("/upload")
async def upload(
    path: str = Query(..., description="Destination directory"),
    file: UploadFile = File(...),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Upload into a directory. This is how music gets to BirdTunes."""
    _require_write(config)
    directory = _resolve(config, path)
    if not directory.is_dir():
        raise ApiError(400, "not_a_directory", "%s is not a directory." % directory)
    # Take only the basename: a browser is free to send "../../etc/cron.d/x" as
    # the filename, and some do it by accident with folder uploads.
    name = os.path.basename(file.filename or "upload.bin") or "upload.bin"
    target = _resolve(config, str(directory / name), must_exist=False)
    written = 0
    try:
        with open(str(target), "wb") as handle:
            while True:
                chunk = await file.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
    except PermissionError:
        raise ApiError(403, "permission_denied", "Cannot write into %s." % directory)
    except OSError as exc:
        raise ApiError(500, "upload_failed", str(exc))
    finally:
        await file.close()
    return {"ok": True, "path": str(target), "bytes": written}


@router.delete("")
async def delete(
    path: str = Query(...),
    recursive: bool = Query(False),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Delete a file, or a directory when ``recursive`` is set.

    A non-empty directory needs the explicit flag. Deleting a folder is the one
    action here with no undo, and a mis-typed path should not take a music
    library with it.
    """
    _require_write(config)
    target = _resolve(config, path)
    if any(target == root for root in _roots(config)):
        raise ApiError(403, "is_a_root", "%s is a root directory." % target)
    try:
        if target.is_dir():
            if recursive:
                shutil.rmtree(str(target))
            else:
                target.rmdir()  # raises if non-empty, which is the point
        else:
            target.unlink()
    except OSError as exc:
        if isinstance(exc, PermissionError):
            raise ApiError(403, "permission_denied", "Cannot delete %s." % target)
        raise ApiError(409, "not_empty", "%s is not empty; pass recursive=true." % target)
    return {"ok": True, "path": str(target)}


__all__ = ["router"]
