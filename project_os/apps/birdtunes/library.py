"""The BirdTunes track store -- scan, search, feedback, playlists, history.

*"Bird tunes sera um app q vc configurara horarios ... ai ele vai tocando
aleatoriamente sua playlist (Musicas do youtube ou mp3 flac ou qualquer merda
com arquivo de musica)"* -- so a track is anything with an audio extension the
user points ``library.paths`` at, plus whatever :mod:`sources` downloads from
YouTube. Nothing here imports mutagen at module level: tag reading degrades to
the filename when the optional library is not installed, which keeps a scan
working on a bare Pi.

Every function takes a :class:`~project_os.db.Database` as its first argument,
same shape as :mod:`project_os.core.helpers` -- the app instance is the only
thing that holds state, this module is just verbs.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from project_os.db import row_to_dict, rows_to_dicts, utcnow_iso

SCHEMA_NAME = "app_birdtunes"

#: The playlist id that always exists and cannot be edited: every usable
#: track. A fresh install with one scanned folder needs to play *something*
#: without the user first building a playlist by hand.
ALL_PLAYLIST_ID = "all"

DEFAULT_EXTENSIONS = [
    ".mp3", ".m4a", ".m4b", ".mp4", ".flac", ".wav", ".wave",
    ".ogg", ".oga", ".opus", ".aac", ".wma", ".aiff", ".aif", ".mka",
]

#: extension -> (container, codec). Best-effort without decoding the file;
#: good enough for the compatibility table in docs/BIRDTUNES.md, and it is
#: only ever used to *warn*, never to silently hide a track without a reason.
_CONTAINER_CODEC = {
    ".mp3": ("mp3", "mp3"),
    ".flac": ("flac", "flac"),
    ".wav": ("wav", "pcm"),
    ".wave": ("wav", "pcm"),
    ".ogg": ("ogg", "vorbis"),
    ".oga": ("ogg", "vorbis"),
    ".opus": ("opus", "opus"),
    ".m4a": ("m4a", "aac"),
    ".m4b": ("m4a", "aac"),
    ".mp4": ("m4a", "aac"),
    ".aac": ("aac", "aac"),
    ".wma": ("wma", "wma"),
    ".aiff": ("aiff", "pcm"),
    ".aif": ("aiff", "pcm"),
    ".mka": ("mka", "unknown"),
    ".webm": ("webm", "opus"),
}

#: Containers each output backend can actually decode, per the table in
#: docs/BIRDTUNES.md section 2. A backend not listed here (``ha_player``,
#: ``local``, ``null``) is assumed broad and only complains at runtime.
_BACKEND_CONTAINERS = {
    "airplay": {"wav", "flac", "mp3", "ogg"},
    "chromecast": {"mp3", "m4a", "aac", "opus", "ogg", "flac", "wav", "webm"},
}

FEEDBACK_ACTIONS = ("like", "dislike", "less", "more", "block", "unblock", "reset")


def schema_statements() -> List[str]:
    return [
        """CREATE TABLE IF NOT EXISTS app_birdtunes_tracks (
               id TEXT PRIMARY KEY,
               path TEXT UNIQUE,
               source TEXT NOT NULL DEFAULT 'local',
               source_id TEXT,
               source_url TEXT,
               title TEXT,
               artist TEXT,
               album TEXT,
               duration REAL DEFAULT 0,
               container TEXT,
               codec TEXT,
               filesize INTEGER,
               thumbnail TEXT,
               bitrate INTEGER,
               tags TEXT,
               added_at TEXT,
               missing INTEGER DEFAULT 0
           )""",
        "CREATE INDEX IF NOT EXISTS idx_app_birdtunes_tracks_source ON app_birdtunes_tracks(source)",
        "CREATE INDEX IF NOT EXISTS idx_app_birdtunes_tracks_missing ON app_birdtunes_tracks(missing)",
        """CREATE TABLE IF NOT EXISTS app_birdtunes_feedback (
               track_id TEXT PRIMARY KEY,
               likes INTEGER DEFAULT 0,
               dislikes INTEGER DEFAULT 0,
               less INTEGER DEFAULT 0,
               plays INTEGER DEFAULT 0,
               skips INTEGER DEFAULT 0,
               completions INTEGER DEFAULT 0,
               blocked INTEGER DEFAULT 0,
               last_played TEXT
           )""",
        """CREATE TABLE IF NOT EXISTS app_birdtunes_history (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               track_id TEXT,
               started_at TEXT,
               ended_at TEXT,
               completed INTEGER DEFAULT 0,
               device TEXT,
               reason TEXT
           )""",
        "CREATE INDEX IF NOT EXISTS idx_app_birdtunes_history_track ON app_birdtunes_history(track_id)",
        "CREATE INDEX IF NOT EXISTS idx_app_birdtunes_history_started ON app_birdtunes_history(started_at)",
        """CREATE TABLE IF NOT EXISTS app_birdtunes_playlists (
               id TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               description TEXT,
               source TEXT DEFAULT 'manual',
               source_url TEXT,
               color TEXT,
               shuffle INTEGER DEFAULT 1,
               created_at TEXT,
               updated_at TEXT
           )""",
        """CREATE TABLE IF NOT EXISTS app_birdtunes_playlist_tracks (
               playlist_id TEXT NOT NULL,
               track_id TEXT NOT NULL,
               position INTEGER DEFAULT 0,
               added_at TEXT,
               PRIMARY KEY (playlist_id, track_id)
           )""",
        "CREATE INDEX IF NOT EXISTS idx_app_birdtunes_pt_playlist ON app_birdtunes_playlist_tracks(playlist_id)",
        """CREATE TABLE IF NOT EXISTS app_birdtunes_imports (
               id TEXT PRIMARY KEY,
               kind TEXT,
               url TEXT,
               title TEXT,
               state TEXT NOT NULL DEFAULT 'queued',
               progress REAL DEFAULT 0,
               total INTEGER DEFAULT 0,
               completed INTEGER DEFAULT 0,
               message TEXT,
               playlist_id TEXT,
               created_at TEXT,
               finished_at TEXT
           )""",
    ]


def register_schema(db: Any) -> None:
    db.register_schema(SCHEMA_NAME, schema_statements())


# --------------------------------------------------------------------------- format table


def container_codec_for(path: str) -> Tuple[str, str]:
    """Best-effort ``(container, codec)`` from the extension alone."""
    _, ext = os.path.splitext(path)
    return _CONTAINER_CODEC.get(ext.lower(), (ext.lower().lstrip("."), "unknown"))


def compatible_with(container: str, codec: str, backend: str) -> bool:
    """Pure function over the format table in docs/BIRDTUNES.md section 2.

    A backend absent from the table (``ha_player``, ``local``, ``null``, or
    anything unrecognised) is assumed broad -- those play whatever the
    delegate/binary handles, so the honest answer is "probably", not "no".
    """
    wanted = _BACKEND_CONTAINERS.get(str(backend or "").lower())
    if wanted is None:
        return True
    return str(container or "").lower() in wanted


# --------------------------------------------------------------------------- metadata


def _title_from_filename(path: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    cleaned = name.replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in cleaned.split()) or name


def _wav_duration(path: str) -> float:
    """The stdlib can read a WAV's own length; no optional dependency needed."""
    import wave

    try:
        with wave.open(path, "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            return round(frames / float(rate), 2)
    except (wave.Error, EOFError, OSError):
        return 0.0


def read_metadata(path: str) -> Dict[str, Any]:
    """Tags when a tag library is installed, filename otherwise.

    ``mutagen`` is optional and imported lazily -- the house rule for every
    third-party dependency BirdTunes leans on -- so a scan still works, with
    poorer titles, on a box that only has the stdlib.
    """
    title = _title_from_filename(path)
    artist = ""
    album = ""
    duration = 0.0
    container, codec = container_codec_for(path)

    try:
        import mutagen  # noqa: F401
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is not None:
            info = getattr(audio, "info", None)
            if info is not None:
                duration = round(float(getattr(info, "length", 0.0) or 0.0), 2)
            tags = audio.tags or {}
            title = _first_tag(tags, "title") or title
            artist = _first_tag(tags, "artist") or artist
            album = _first_tag(tags, "album") or album
    except ImportError:
        pass
    except Exception:  # pragma: no cover - a corrupt file must not kill a scan
        pass

    if not duration and container == "wav":
        duration = _wav_duration(path)

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "container": container,
        "codec": codec,
    }


def _first_tag(tags: Any, key: str) -> str:
    try:
        value = tags.get(key)
    except AttributeError:  # pragma: no cover - non-dict-like tag object
        return ""
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


# --------------------------------------------------------------------------- scan


def _track_id_for(path: str) -> str:
    # Stable across rescans: the same file always maps to the same id, so
    # feedback and history survive a rescan instead of resetting to zero.
    return uuid.uuid5(uuid.NAMESPACE_URL, "birdtunes:local:%s" % path).hex


def scan(
    db: Any,
    paths: Iterable[str],
    extensions: Optional[Iterable[str]] = None,
    follow_symlinks: bool = False,
) -> Dict[str, int]:
    """Walk ``paths`` for audio files and upsert them into the library.

    Files are never moved or copied -- *"nada hardcoded"* extends to the
    user's own folder layout. A file that disappears since the last scan is
    marked ``missing`` rather than deleted, so its likes/dislikes survive a
    temporarily unplugged drive.
    """
    exts = set(e.lower() for e in (extensions or DEFAULT_EXTENSIONS))
    seen_paths = set()  # type: set
    added = 0
    updated = 0

    for root in paths or []:
        root = os.path.expanduser(str(root))
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=bool(follow_symlinks)):
            for filename in filenames:
                _, ext = os.path.splitext(filename)
                if ext.lower() not in exts:
                    continue
                full = os.path.join(dirpath, filename)
                seen_paths.add(full)
                existing = db.one(
                    "SELECT id, missing FROM app_birdtunes_tracks WHERE path = ?", (full,)
                )
                if existing is not None:
                    if existing["missing"]:
                        db.execute(
                            "UPDATE app_birdtunes_tracks SET missing = 0 WHERE id = ?",
                            (existing["id"],),
                        )
                        updated += 1
                    continue
                meta = read_metadata(full)
                track_id = _track_id_for(full)
                try:
                    filesize = os.path.getsize(full)
                except OSError:
                    filesize = None
                db.execute(
                    "INSERT INTO app_birdtunes_tracks "
                    "(id, path, source, title, artist, album, duration, container, codec, "
                    " filesize, added_at, missing) "
                    "VALUES (?, ?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(path) DO NOTHING",
                    (
                        track_id, full, meta["title"], meta["artist"], meta["album"],
                        meta["duration"], meta["container"], meta["codec"], filesize,
                        utcnow_iso(),
                    ),
                )
                db.execute(
                    "INSERT OR IGNORE INTO app_birdtunes_feedback (track_id) VALUES (?)",
                    (track_id,),
                )
                added += 1

    missing = 0
    for row in db.query(
        "SELECT id, path FROM app_birdtunes_tracks WHERE source = 'local' AND missing = 0"
    ):
        if row["path"] not in seen_paths:
            db.execute(
                "UPDATE app_birdtunes_tracks SET missing = 1 WHERE id = ?", (row["id"],)
            )
            missing += 1

    total = db.scalar("SELECT COUNT(*) FROM app_birdtunes_tracks", default=0)
    return {"added": added, "updated": updated, "missing": missing, "total": int(total or 0)}


def add_url_track(db: Any, url: str, title: str = "") -> Dict[str, Any]:
    """A direct internet-radio stream: played without downloading, never shuffled."""
    track_id = _track_id_for(url)
    db.execute(
        "INSERT INTO app_birdtunes_tracks "
        "(id, path, source, source_url, title, added_at, missing) "
        "VALUES (?, '', 'url', ?, ?, ?, 0) "
        "ON CONFLICT(path) DO NOTHING",
        (track_id, url, title or url, utcnow_iso()),
    )
    db.execute("INSERT OR IGNORE INTO app_birdtunes_feedback (track_id) VALUES (?)", (track_id,))
    return get_track(db, track_id) or {}


# --------------------------------------------------------------------------- reads


_DEFAULT_FEEDBACK = {
    "likes": 0, "dislikes": 0, "less": 0, "plays": 0, "skips": 0,
    "completions": 0, "blocked": 0, "last_played": None,
}


def _shape_track(row: Any) -> Dict[str, Any]:
    data = row_to_dict(row) or {}
    data["missing"] = bool(data.get("missing"))
    return data


def get_track(db: Any, track_id: str) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM app_birdtunes_tracks WHERE id = ?", (track_id,))
    return _shape_track(row) if row is not None else None


def list_tracks(
    db: Any,
    search: str = "",
    sort: str = "title",
    playlist_id: Optional[str] = None,
    include_missing: bool = True,
) -> List[Dict[str, Any]]:
    sql = "SELECT t.* FROM app_birdtunes_tracks t"
    params = []  # type: List[Any]
    clauses = []  # type: List[str]
    if playlist_id and playlist_id != ALL_PLAYLIST_ID:
        sql += " JOIN app_birdtunes_playlist_tracks pt ON pt.track_id = t.id"
        clauses.append("pt.playlist_id = ?")
        params.append(playlist_id)
    if search:
        clauses.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?)")
        like = "%%%s%%" % search
        params.extend([like, like, like])
    if not include_missing:
        clauses.append("t.missing = 0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    order = {
        "title": "t.title COLLATE NOCASE",
        "artist": "t.artist COLLATE NOCASE",
        "added": "t.added_at DESC",
        "duration": "t.duration DESC",
    }.get(sort, "t.title COLLATE NOCASE")
    sql += " ORDER BY %s" % order
    tracks = [_shape_track(row) for row in db.query(sql, params)]

    # Feedback travels with the track. The list is where a person decides
    # whether they like something, so the screen needs the counts and the
    # blocked flag there -- one query for all of them, not one request per row.
    rows = db.query("SELECT * FROM app_birdtunes_feedback")
    by_track = {}  # type: Dict[str, Dict[str, Any]]
    for row in rows:
        data = row_to_dict(row) or {}
        data["blocked"] = bool(data.get("blocked"))
        by_track[data.get("track_id", "")] = data
    for track in tracks:
        track["feedback"] = by_track.get(track["id"], dict(_DEFAULT_FEEDBACK, track_id=track["id"]))
    return tracks


def delete_track(db: Any, track_id: str) -> bool:
    row = db.one("SELECT id FROM app_birdtunes_tracks WHERE id = ?", (track_id,))
    if row is None:
        return False
    with db.transaction():
        db.execute("DELETE FROM app_birdtunes_tracks WHERE id = ?", (track_id,))
        db.execute("DELETE FROM app_birdtunes_feedback WHERE track_id = ?", (track_id,))
        db.execute(
            "DELETE FROM app_birdtunes_playlist_tracks WHERE track_id = ?", (track_id,)
        )
    return True


# --------------------------------------------------------------------------- feedback


def feedback_for(db: Any, track_id: str) -> Dict[str, Any]:
    row = db.one("SELECT * FROM app_birdtunes_feedback WHERE track_id = ?", (track_id,))
    if row is None:
        return dict(_DEFAULT_FEEDBACK, track_id=track_id)
    data = row_to_dict(row) or {}
    data["blocked"] = bool(data.get("blocked"))
    return data


def apply_feedback(db: Any, track_id: str, action: str) -> Dict[str, Any]:
    """*"tem botao de curtir musicas, recomendar menos e etc"* -- this is that button."""
    action = str(action or "").lower()
    if action not in FEEDBACK_ACTIONS:
        raise ValueError("unknown feedback action: %r" % action)
    db.execute("INSERT OR IGNORE INTO app_birdtunes_feedback (track_id) VALUES (?)", (track_id,))
    if action == "like":
        db.execute(
            "UPDATE app_birdtunes_feedback SET likes = likes + 1 WHERE track_id = ?", (track_id,)
        )
    elif action == "dislike":
        db.execute(
            "UPDATE app_birdtunes_feedback SET dislikes = dislikes + 1 WHERE track_id = ?",
            (track_id,),
        )
    elif action == "less":
        db.execute(
            "UPDATE app_birdtunes_feedback SET less = less + 1 WHERE track_id = ?", (track_id,)
        )
    elif action == "more":
        # The undo of "less"/"dislike": whichever discouragement is present
        # backs off by one, so a misclick has a way back.
        current = feedback_for(db, track_id)
        if current["less"] > 0:
            db.execute(
                "UPDATE app_birdtunes_feedback SET less = less - 1 WHERE track_id = ?",
                (track_id,),
            )
        elif current["dislikes"] > 0:
            db.execute(
                "UPDATE app_birdtunes_feedback SET dislikes = dislikes - 1 WHERE track_id = ?",
                (track_id,),
            )
    elif action == "block":
        db.execute(
            "UPDATE app_birdtunes_feedback SET blocked = 1 WHERE track_id = ?", (track_id,)
        )
    elif action == "unblock":
        db.execute(
            "UPDATE app_birdtunes_feedback SET blocked = 0 WHERE track_id = ?", (track_id,)
        )
    elif action == "reset":
        db.execute(
            "UPDATE app_birdtunes_feedback SET likes = 0, dislikes = 0, less = 0, "
            "blocked = 0 WHERE track_id = ?",
            (track_id,),
        )
    return feedback_for(db, track_id)


def record_play_start(db: Any, track_id: str, device: str = "") -> int:
    now = utcnow_iso()
    db.execute("INSERT OR IGNORE INTO app_birdtunes_feedback (track_id) VALUES (?)", (track_id,))
    db.execute(
        "UPDATE app_birdtunes_feedback SET plays = plays + 1, last_played = ? WHERE track_id = ?",
        (now, track_id),
    )
    cursor = db.execute(
        "INSERT INTO app_birdtunes_history (track_id, started_at, device) VALUES (?, ?, ?)",
        (track_id, now, device),
    )
    return int(cursor.lastrowid)


def record_play_end(
    db: Any, history_id: int, track_id: str, completed: bool, reason: str = ""
) -> None:
    db.execute(
        "UPDATE app_birdtunes_history SET ended_at = ?, completed = ?, reason = ? WHERE id = ?",
        (utcnow_iso(), 1 if completed else 0, reason, history_id),
    )
    if completed:
        db.execute(
            "UPDATE app_birdtunes_feedback SET completions = completions + 1 WHERE track_id = ?",
            (track_id,),
        )
    else:
        db.execute(
            "UPDATE app_birdtunes_feedback SET skips = skips + 1 WHERE track_id = ?",
            (track_id,),
        )


def history(db: Any, limit: int = 50) -> List[Dict[str, Any]]:
    rows = db.query(
        "SELECT h.*, t.title, t.artist FROM app_birdtunes_history h "
        "LEFT JOIN app_birdtunes_tracks t ON t.id = h.track_id "
        "ORDER BY h.id DESC LIMIT ?",
        (int(limit),),
    )
    return rows_to_dicts(rows)


# --------------------------------------------------------------------------- candidate set


def candidate_set(
    db: Any, playlist_id: Optional[str], backend: str = ""
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Tracks a session could actually play, and why there are none.

    Returns ``(tracks, reason)``; ``reason`` is one of ``None``, ``"no_tracks"``
    or ``"all_incompatible"`` -- section 6 of docs/BIRDTUNES.md is explicit that
    an empty queue must never surface as a bare "stopped".
    """
    tracks = list_tracks(db, playlist_id=playlist_id, include_missing=False)
    tracks = [t for t in tracks if not feedback_for(db, t["id"]).get("blocked")]
    if not tracks:
        return [], "no_tracks"
    compatible = [t for t in tracks if compatible_with(t.get("container", ""), t.get("codec", ""), backend)]
    if not compatible:
        return [], "all_incompatible"
    return compatible, None


# --------------------------------------------------------------------------- playlists


def _shape_playlist(row: Any) -> Dict[str, Any]:
    data = row_to_dict(row) or {}
    data["shuffle"] = bool(data.get("shuffle", 1))
    return data


def _virtual_all_playlist(db: Any) -> Dict[str, Any]:
    count = db.scalar(
        "SELECT COUNT(*) FROM app_birdtunes_tracks WHERE missing = 0", default=0
    )
    duration = db.scalar(
        "SELECT COALESCE(SUM(duration), 0) FROM app_birdtunes_tracks WHERE missing = 0",
        default=0,
    )
    return {
        "id": ALL_PLAYLIST_ID,
        "name": "Todas as faixas",
        "description": "Todas as faixas do acervo que não estão bloqueadas nem sumidas.",
        "source": "virtual",
        "source_url": None,
        "color": "",
        "shuffle": True,
        "created_at": None,
        "updated_at": None,
        "track_count": int(count or 0),
        "duration": float(duration or 0),
        "virtual": True,
    }


def list_playlists(db: Any) -> List[Dict[str, Any]]:
    rows = db.query(
        "SELECT p.*, "
        " (SELECT COUNT(*) FROM app_birdtunes_playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count, "
        " (SELECT COALESCE(SUM(t.duration), 0) FROM app_birdtunes_playlist_tracks pt "
        "    JOIN app_birdtunes_tracks t ON t.id = pt.track_id WHERE pt.playlist_id = p.id) AS duration "
        "FROM app_birdtunes_playlists p ORDER BY p.name COLLATE NOCASE"
    )
    out = [_virtual_all_playlist(db)]
    for row in rows:
        data = _shape_playlist(row)
        data["virtual"] = False
        out.append(data)
    return out


def get_playlist(db: Any, playlist_id: str) -> Optional[Dict[str, Any]]:
    if playlist_id == ALL_PLAYLIST_ID:
        return _virtual_all_playlist(db)
    row = db.one(
        "SELECT p.*, "
        " (SELECT COUNT(*) FROM app_birdtunes_playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count, "
        " (SELECT COALESCE(SUM(t.duration), 0) FROM app_birdtunes_playlist_tracks pt "
        "    JOIN app_birdtunes_tracks t ON t.id = pt.track_id WHERE pt.playlist_id = p.id) AS duration "
        "FROM app_birdtunes_playlists p WHERE p.id = ?",
        (playlist_id,),
    )
    if row is None:
        return None
    data = _shape_playlist(row)
    data["virtual"] = False
    return data


def create_playlist(
    db: Any,
    name: str,
    description: str = "",
    color: str = "",
    shuffle: bool = True,
    source: str = "manual",
    source_url: str = "",
) -> Dict[str, Any]:
    playlist_id = uuid.uuid4().hex
    now = utcnow_iso()
    db.execute(
        "INSERT INTO app_birdtunes_playlists "
        "(id, name, description, source, source_url, color, shuffle, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (playlist_id, name, description, source, source_url, color, 1 if shuffle else 0, now, now),
    )
    return get_playlist(db, playlist_id) or {}


def update_playlist(db: Any, playlist_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    if playlist_id == ALL_PLAYLIST_ID:
        raise ValueError("the \"all\" playlist cannot be edited")
    if get_playlist(db, playlist_id) is None:
        return None
    allowed = {"name", "description", "color", "shuffle"}
    sets = []  # type: List[str]
    params = []  # type: List[Any]
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        sets.append("%s = ?" % key)
        params.append(1 if (key == "shuffle" and value) else (0 if key == "shuffle" else value))
    if sets:
        sets.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(playlist_id)
        db.execute(
            "UPDATE app_birdtunes_playlists SET %s WHERE id = ?" % ", ".join(sets), params
        )
    return get_playlist(db, playlist_id)


def delete_playlist(db: Any, playlist_id: str) -> bool:
    if playlist_id == ALL_PLAYLIST_ID:
        raise ValueError("the \"all\" playlist cannot be deleted")
    row = db.one("SELECT id FROM app_birdtunes_playlists WHERE id = ?", (playlist_id,))
    if row is None:
        return False
    with db.transaction():
        db.execute("DELETE FROM app_birdtunes_playlists WHERE id = ?", (playlist_id,))
        db.execute(
            "DELETE FROM app_birdtunes_playlist_tracks WHERE playlist_id = ?", (playlist_id,)
        )
    return True


def playlist_tracks(db: Any, playlist_id: str) -> List[Dict[str, Any]]:
    if playlist_id == ALL_PLAYLIST_ID:
        return list_tracks(db, sort="title")
    rows = db.query(
        "SELECT t.* FROM app_birdtunes_playlist_tracks pt "
        "JOIN app_birdtunes_tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ? ORDER BY pt.position",
        (playlist_id,),
    )
    return [_shape_track(row) for row in rows]


def add_tracks_to_playlist(db: Any, playlist_id: str, track_ids: Iterable[str]) -> int:
    if playlist_id == ALL_PLAYLIST_ID:
        raise ValueError("the \"all\" playlist already contains every track")
    row = db.one("SELECT id FROM app_birdtunes_playlists WHERE id = ?", (playlist_id,))
    if row is None:
        raise KeyError(playlist_id)
    start = db.scalar(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM app_birdtunes_playlist_tracks "
        "WHERE playlist_id = ?",
        (playlist_id,),
        default=0,
    )
    added = 0
    now = utcnow_iso()
    with db.transaction():
        for offset, track_id in enumerate(track_ids):
            db.execute(
                "INSERT OR IGNORE INTO app_birdtunes_playlist_tracks "
                "(playlist_id, track_id, position, added_at) VALUES (?, ?, ?, ?)",
                (playlist_id, track_id, int(start) + offset, now),
            )
            added += 1
    return added


def remove_tracks_from_playlist(db: Any, playlist_id: str, track_ids: Iterable[str]) -> int:
    removed = 0
    with db.transaction():
        for track_id in track_ids:
            cursor = db.execute(
                "DELETE FROM app_birdtunes_playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                (playlist_id, track_id),
            )
            removed += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    return removed


def reorder_playlist(db: Any, playlist_id: str, track_ids: List[str]) -> None:
    if playlist_id == ALL_PLAYLIST_ID:
        raise ValueError("the \"all\" playlist has no order of its own")
    with db.transaction():
        for position, track_id in enumerate(track_ids):
            db.execute(
                "UPDATE app_birdtunes_playlist_tracks SET position = ? "
                "WHERE playlist_id = ? AND track_id = ?",
                (position, playlist_id, track_id),
            )


__all__ = [
    "ALL_PLAYLIST_ID",
    "DEFAULT_EXTENSIONS",
    "FEEDBACK_ACTIONS",
    "SCHEMA_NAME",
    "add_tracks_to_playlist",
    "add_url_track",
    "apply_feedback",
    "candidate_set",
    "compatible_with",
    "container_codec_for",
    "create_playlist",
    "delete_playlist",
    "delete_track",
    "feedback_for",
    "get_playlist",
    "get_track",
    "history",
    "list_playlists",
    "list_tracks",
    "playlist_tracks",
    "read_metadata",
    "record_play_end",
    "record_play_start",
    "register_schema",
    "remove_tracks_from_playlist",
    "reorder_playlist",
    "scan",
    "schema_statements",
    "update_playlist",
]
