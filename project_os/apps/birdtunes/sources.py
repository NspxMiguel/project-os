"""YouTube import through yt-dlp -- download and cache, never stream live.

*"Musicas do youtube ou mp3 flac ou qualquer merda com arquivo de musica"* --
YouTube is a source, not a special case: once downloaded a track is a local
file like any other and the rest of the app never knows where it came from.

Section 3 of docs/BIRDTUNES.md is normative here: download once, dedupe on
the YouTube video id, keep going when one item in a playlist fails, and never
shell out to ``youtube-dl``/``yt-dlp`` as a subprocess -- ``yt_dlp`` is a pure
Python library, invoked in-process, imported lazily so a box without it still
starts (the import UI just explains why it is disabled).
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from project_os.db import utcnow_iso

from . import library

INSTALL_HINT = "pip install yt-dlp (YouTube import is an optional dependency)"

IMPORT_STATES = ("queued", "running", "done", "error", "cancelled")


class SourceError(Exception):
    """A source-side failure with an actionable hint, same shape as PlayerError."""

    def __init__(self, message: str, code: str = "source_error", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint

    def as_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message, "hint": self.hint}


def _yt_dlp() -> Any:
    try:
        import yt_dlp
        import yt_dlp.utils  # noqa: F401
    except ImportError as exc:
        raise SourceError(
            "yt-dlp is not installed, so YouTube import is unavailable.",
            code="ytdlp_missing",
            hint=INSTALL_HINT,
        ) from exc
    return yt_dlp


def available() -> bool:
    """Whether YouTube import can run at all.

    Already-imported wins over find_spec: a module sitting in ``sys.modules``
    is importable by definition, and find_spec raises ValueError on any module
    whose ``__spec__`` is None -- which is true of anything installed by hand
    into sys.modules, tests included. Asking find_spec first turned a module
    that is right there into "not installed".
    """
    import importlib.util
    import sys

    if "yt_dlp" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


#: Every shape of YouTube link a person actually pastes.
_YOUTUBE_ID = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})"
)


def youtube_video_id(url: str) -> str:
    """The video id in ``url``, or "" -- no network, no yt-dlp.

    Used by the "play it on the television without downloading anything" path,
    which must work on a box where yt-dlp was never installed.
    """
    text = str(url or "").strip()
    if re.match(r"^[A-Za-z0-9_-]{11}$", text):
        return text  # already an id
    match = _YOUTUBE_ID.search(text)
    return match.group(1) if match else ""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


_ERROR_PREFIX = re.compile(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]{6,}:\s*)?")


def _tidy_error(message: Any) -> str:
    """yt-dlp's line, minus the plumbing.

    "ERROR: [youtube] Qk14auV_OJs: The page needs to be reloaded." is a sentence
    with the useful part in the middle; the screen shows this, so it says what
    happened and not which extractor said it.
    """
    text = str(message or "").strip()
    text = _ERROR_PREFIX.sub("", text)
    marker = ". Use --cookies"
    if marker in text:
        text = text.split(marker, 1)[0] + "."
    return text.strip()


class _ErrorCatcher(object):
    """Keeps yt-dlp's last error line.

    ``ignoreerrors`` is on so one bad video in a playlist does not sink the
    whole import -- but it also means a refused video comes back as ``None``
    with no exception, and the job used to report "Nothing new to import.",
    which reads as "you already had it". A private video, an age gate or a
    region block all end up here, and the user deserves the actual sentence.
    """

    def __init__(self) -> None:
        self.last = ""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        text = _tidy_error(message)
        if text:
            self.last = text


def _base_options(dest_dir: str, quality: str, noplaylist: bool) -> Dict[str, Any]:
    postprocessors = []
    if ffmpeg_available():
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}
        )
    return {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(dest_dir, "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": noplaylist,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "postprocessors": postprocessors,
    }


# Which YouTube front-end yt-dlp pretends to be. The default one gets refused
# often ("The page needs to be reloaded."), and the same video downloads fine as
# the Android client -- verified on this machine minutes after the web client
# was blocked. Tried in order, first one that works wins.
PLAYER_CLIENTS = ("", "android", "ios", "web_safari", "tv")


def _download_once(yt_dlp: Any, opts: Dict[str, Any], url: str, client: str):
    """One download attempt as one player client. Returns (info, ydl, error)."""
    attempt = dict(opts)
    if client:
        extractor_args = dict(attempt.get("extractor_args") or {})
        extractor_args["youtube"] = {"player_client": [client]}
        attempt["extractor_args"] = extractor_args
    catcher = _ErrorCatcher()
    attempt["logger"] = catcher
    ydl = yt_dlp.YoutubeDL(attempt)
    try:
        info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        return None, ydl, _tidy_error(exc)
    if info is None:
        return None, ydl, catcher.last
    return info, ydl, ""


def download_entry(yt_dlp: Any, opts: Dict[str, Any], url: str,
                   clients: Optional[Iterable[str]] = None):
    """Download ``url``, falling back through the player clients.

    Returns ``(info, ydl, error)``; ``info`` is None when every client failed,
    and ``error`` is the first real sentence YouTube gave -- not a guess.
    """
    first_error = ""
    for client in (clients if clients is not None else PLAYER_CLIENTS):
        info, ydl, error = _download_once(yt_dlp, opts, url, client)
        if info is not None:
            return info, ydl, ""
        if error and not first_error:
            first_error = error
    return None, None, first_error


def _iter_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = info.get("entries")
    if entries is None:
        return [info] if info else []
    return [e for e in entries if e]


def preview(url: str) -> Dict[str, Any]:
    """A flat listing of what ``url`` points at, without downloading anything.

    Used by ``GET /import/preview`` so the UI can show "47 tracks found"
    before committing to a long download -- ``extract_flat`` is exactly the
    knob yt-dlp offers for that.
    """
    yt_dlp = _yt_dlp()
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "extract_flat": "in_playlist", "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise SourceError("Could not read %s" % url, code="preview_failed", hint=str(exc)) from exc
    entries = _iter_entries(info or {})
    is_playlist = bool((info or {}).get("entries") is not None)
    items = [
        {
            "id": e.get("id", ""),
            "title": e.get("title", "") or e.get("id", ""),
            "duration": float(e.get("duration") or 0.0),
            "uploader": e.get("uploader", ""),
            "thumbnail": e.get("thumbnail", ""),
        }
        for e in entries
    ]
    return {
        "kind": "playlist" if is_playlist else "video",
        "title": (info or {}).get("title", "") or (items[0]["title"] if items else url),
        "count": len(items),
        "items": items,
    }


def create_job(
    db: Any,
    url: str,
    kind: str = "video",
    playlist_id: Optional[str] = None,
    title: str = "",
) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO app_birdtunes_imports "
        "(id, kind, url, title, state, progress, total, completed, message, playlist_id, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', 0, 0, 0, '', ?, ?)",
        (job_id, kind, url, title, playlist_id, utcnow_iso()),
    )
    return get_job(db, job_id) or {}


def get_job(db: Any, job_id: str) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM app_birdtunes_imports WHERE id = ?", (job_id,))
    from project_os.db import row_to_dict

    return row_to_dict(row)


def list_jobs(db: Any, limit: int = 50) -> List[Dict[str, Any]]:
    from project_os.db import rows_to_dicts

    return rows_to_dicts(
        db.query("SELECT * FROM app_birdtunes_imports ORDER BY created_at DESC LIMIT ?", (limit,))
    )


def _update_job(db: Any, job_id: str, **fields: Any) -> None:
    sets = []  # type: List[str]
    params = []  # type: List[Any]
    for key, value in fields.items():
        sets.append("%s = ?" % key)
        params.append(value)
    params.append(job_id)
    db.execute("UPDATE app_birdtunes_imports SET %s WHERE id = ?" % ", ".join(sets), params)


def cancel_job(db: Any, job_id: str) -> bool:
    job = get_job(db, job_id)
    if job is None or job["state"] not in ("queued", "running"):
        return False
    _update_job(db, job_id, state="cancelled", finished_at=utcnow_iso())
    return True


def run_job(
    db: Any,
    job_id: str,
    dest_dir: str,
    quality: str = "192",
    is_cancelled: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    clients: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Do the actual download, one item at a time, honestly reporting failures.

    Runs synchronously and is meant to be called from ``run_in_executor`` --
    both ``extract_info`` and the download it triggers block. ``is_cancelled``
    is polled between items so a long playlist import can be stopped midway;
    partial output from the item in flight is left for the OS temp cleanup
    yt-dlp itself does on a cancelled download.
    """
    job = get_job(db, job_id)
    if job is None:
        raise KeyError(job_id)
    os.makedirs(dest_dir, exist_ok=True)
    yt_dlp = _yt_dlp()
    _update_job(db, job_id, state="running")

    noplaylist = job["kind"] != "playlist"
    opts = _base_options(dest_dir, quality, noplaylist)
    catcher = _ErrorCatcher()
    opts["logger"] = catcher
    added_tracks = []  # type: List[Dict[str, Any]]
    # Everything the URL resolved to, downloaded now or already here. Adding a
    # link you once imported to a playlist has to work: "already in the library"
    # is not a reason to leave the playlist empty.
    resolved_ids = []  # type: List[str]
    error_message = ""

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=False)
            entries = _iter_entries(info or {})
            total = len(entries) or 1
            _update_job(db, job_id, total=total)
            for index, entry in enumerate(entries or [info or {}]):
                if is_cancelled is not None and is_cancelled():
                    _update_job(db, job_id, state="cancelled", finished_at=utcnow_iso())
                    return get_job(db, job_id) or {}
                video_id = entry.get("id", "")
                existing = db.one(
                    "SELECT id FROM app_birdtunes_tracks WHERE source = 'youtube' AND source_id = ?",
                    (video_id,),
                )
                if existing is not None:
                    resolved_ids.append(existing["id"])
                if existing is None:
                    downloaded, used_ydl, failure = download_entry(
                        yt_dlp, opts, entry.get("webpage_url") or job["url"], clients)
                    if downloaded is None and not error_message:
                        error_message = failure or (
                            "YouTube refused %s and gave no reason." % (entry.get("title") or video_id))
                    if downloaded is not None:
                        track = _store_downloaded(db, used_ydl, downloaded, quality)
                        if track is not None:
                            added_tracks.append(track)
                            resolved_ids.append(track["id"])
                progress = round((index + 1) / float(total), 3)
                _update_job(db, job_id, progress=progress, completed=index + 1)
                if on_progress is not None:
                    on_progress({"job_id": job_id, "progress": progress, "index": index})
    except yt_dlp.utils.DownloadError as exc:
        _update_job(db, job_id, state="error", message=str(exc), finished_at=utcnow_iso())
        return get_job(db, job_id) or {}
    except Exception as exc:  # pragma: no cover - yt-dlp internals are not ours to predict
        _update_job(db, job_id, state="error", message=str(exc), finished_at=utcnow_iso())
        return get_job(db, job_id) or {}

    added_to_playlist = 0
    if job.get("playlist_id") and resolved_ids:
        added_to_playlist = library.add_tracks_to_playlist(db, job["playlist_id"], resolved_ids)

    # "done" has to mean something arrived. yt-dlp runs with ignoreerrors, so a
    # video that YouTube refused leaves an empty result and no exception -- and
    # the job used to report success with "Nothing new to import.", which reads
    # like "you already had it" and is exactly the wrong thing when the download
    # was blocked. Nothing new *and* an error is an error.
    if not added_tracks and error_message:
        _update_job(db, job_id, state="error", message=error_message, finished_at=utcnow_iso())
        return get_job(db, job_id) or {}

    if added_tracks:
        message = ""
    elif added_to_playlist:
        message = "Already in the library -- added to the playlist."
    elif resolved_ids:
        message = "Already in the library."
    else:
        message = "Nothing new to import."
    _update_job(db, job_id, state="done", message=message, finished_at=utcnow_iso())
    return get_job(db, job_id) or {}


def _store_downloaded(db: Any, ydl: Any, info: Dict[str, Any], quality: str) -> Optional[Dict[str, Any]]:
    video_id = info.get("id", "")
    if not video_id:
        return None
    filename = ydl.prepare_filename(info)
    if ffmpeg_available():
        # FFmpegExtractAudio rewrites the extension after the postprocessor runs.
        filename = os.path.splitext(filename)[0] + ".mp3"
    container, codec = library.container_codec_for(filename)
    track_id = uuid.uuid5(uuid.NAMESPACE_URL, "birdtunes:youtube:%s" % video_id).hex
    duration = float(info.get("duration") or 0.0)
    title = info.get("title", "") or video_id
    artist = info.get("uploader", "") or ""
    db.execute(
        "INSERT INTO app_birdtunes_tracks "
        "(id, path, source, source_id, source_url, title, artist, duration, container, codec, "
        " thumbnail, added_at, missing) "
        "VALUES (?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(path) DO NOTHING",
        (
            track_id, filename, video_id, info.get("webpage_url", ""), title, artist,
            duration, container, codec, info.get("thumbnail", ""), utcnow_iso(),
        ),
    )
    db.execute("INSERT OR IGNORE INTO app_birdtunes_feedback (track_id) VALUES (?)", (track_id,))
    return library.get_track(db, track_id)


__all__ = [
    "IMPORT_STATES",
    "INSTALL_HINT",
    "SourceError",
    "available",
    "cancel_job",
    "create_job",
    "ffmpeg_available",
    "get_job",
    "list_jobs",
    "preview",
    "run_job",
    "youtube_video_id",
]
