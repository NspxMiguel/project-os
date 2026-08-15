"""setup(ctx) -> AppInstance: the part that actually runs.

Everything else in this package is pure or optional. This module is where
*"ele simplesmente se conecta ao meu apple tv 4k ... ou chromecast (configuravel)
e coloca musica pros meu passarinho ouvir"* becomes real: it owns the queue,
the currently selected :class:`~.players.base.Player`, the scheduler loop
that decides when a session should be running, and the FastAPI router other
apps and the panel talk to.

House pattern (matching ``apps/whatsapp-bot/app.py``): a plain
``AppInstance`` subclass builds its own router in ``__init__`` via
``build_router(self)``, bodies are read as plain ``Dict[str, Any]`` rather
than pydantic models, and every authenticated route depends on
``auth.require_auth`` -- except ``/media/{track_id}``, which a Chromecast
fetches with no session cookie and is instead protected by its own signed,
expiring token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

import anyio

from project_os import auth, paths
from project_os.core.plugins import AppContext, AppInstance
from project_os.errors import ApiError

from . import library, recommender, safety, scheduler, sources
from .players.base import Player, PlayerError, Track, content_type_for
from .players.airplay import AirPlayPlayer
from .players.chromecast import ChromecastPlayer
from .players.null import NullPlayer

APP_ID = "birdtunes"

#: Quiet by default. This plays to a bird in a room, unattended, on a schedule;
#: the volume you would pick for music you are listening to is the wrong one.
#: The slider in the panel moves it, and output.max_volume caps it.
DEFAULT_VOLUME = 0.15

#: kind -> Player subclass. "null" is always available; the rest degrade
#: gracefully via their own available() when the optional library is missing.
PLAYER_CLASSES = {
    NullPlayer.kind: NullPlayer,
    AirPlayPlayer.kind: AirPlayPlayer,
    ChromecastPlayer.kind: ChromecastPlayer,
}

#: Media links are handed to a Chromecast on the LAN; an hour is generous
#: enough for a long track plus network hiccups, short enough that a leaked
#: URL is not useful for long.
MEDIA_URL_TTL = 3600
#: Recently-played ids kept for the recommender's recency penalty -- see
#: recommender.py's docstring: the window itself is a config knob, but the
#: list has to be at least that long to matter.
MAX_RECENT_IDS = 32


def _sign_media(secret: str, track_id: str, exp: int) -> str:
    message = "%s:%s" % (track_id, exp)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_media(secret: str, track_id: str, exp: int, token: str) -> bool:
    if exp < int(time.time()):
        return False
    expected = _sign_media(secret, track_id, exp)
    return hmac.compare_digest(expected, str(token or ""))


def _empty_reason_message(reason: Optional[str]) -> str:
    return {
        None: "",
        "no_tracks": "Essa playlist ainda não tem faixa que dê para tocar.",
        "all_incompatible": "Todas as faixas daqui estão num formato que a saída atual não toca.",
        "output_unreachable": "A saída de som não está acessível agora.",
        "quiet_hours": "Agora é hora do silêncio, então o BirdTunes não vai tocar.",
    }.get(reason, "Não tem nada para tocar agora.")


def build_router(instance: "BirdTunesApp") -> APIRouter:
    router = APIRouter()
    auth_dep = Depends(auth.require_auth)

    # -- playback ----------------------------------------------------------
    @router.get("/status")
    async def get_status(_: Any = auth_dep) -> Dict[str, Any]:
        return instance.status()

    @router.post("/play")
    async def post_play(body: Dict[str, Any] = None, _: Any = auth_dep) -> Dict[str, Any]:
        body = body or {}
        return await instance.play(track_id=body.get("track_id"), playlist_id=body.get("playlist_id"))

    @router.post("/pause")
    async def post_pause(_: Any = auth_dep) -> Dict[str, Any]:
        return await instance.pause()

    @router.post("/resume")
    async def post_resume(_: Any = auth_dep) -> Dict[str, Any]:
        return await instance.resume()

    @router.post("/stop")
    async def post_stop(_: Any = auth_dep) -> Dict[str, Any]:
        return await instance.stop_playback()

    @router.post("/next")
    async def post_next(_: Any = auth_dep) -> Dict[str, Any]:
        return await instance.next_track()

    @router.post("/volume")
    async def post_volume(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        # Both spellings, because both are the obvious one, and a missing or
        # unreadable number is an error: clamp_volume turns None into 0.0, so
        # the old code answered 200 and silently muted the speaker.
        raw = body.get("value", body.get("volume"))
        if raw is None:
            raise ApiError(400, "bad_request", "Send the volume as \"value\" (0.0 to 1.0).")
        try:
            float(raw)
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "O volume tem que ser um número entre 0 e 1.")
        return await instance.set_volume(raw)

    # -- library -------------------------------------------------------
    @router.get("/library")
    async def get_library(
        search: str = "", sort: str = "title", playlist_id: str = "", _: Any = auth_dep
    ) -> Dict[str, Any]:
        tracks = library.list_tracks(instance.ctx.db, search=search, sort=sort, playlist_id=playlist_id or None)
        return {"tracks": tracks}

    @router.post("/library/scan")
    async def post_scan(_: Any = auth_dep) -> Dict[str, Any]:
        return instance.scan_library()

    @router.post("/library/add-url")
    async def post_add_url(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        url = str(body.get("url") or "").strip()
        if not url:
            raise ApiError(400, "bad_request", "Send a URL.")
        track = library.add_url_track(instance.ctx.db, url, body.get("title", ""))
        instance.ctx.emit("library", {"kind": "url_added", "track_id": track.get("id")})
        return {"track": track}

    @router.delete("/library/{track_id}")
    async def delete_library_track(track_id: str, _: Any = auth_dep) -> Dict[str, Any]:
        ok = library.delete_track(instance.ctx.db, track_id)
        if not ok:
            raise ApiError(404, "not_found", "Faixa não encontrada.")
        instance.ctx.emit("library", {"kind": "track_deleted", "track_id": track_id})
        return {"deleted": True}

    @router.post("/tracks/{track_id}/feedback")
    async def post_feedback(track_id: str, body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        action = str(body.get("action") or "")
        try:
            feedback = library.apply_feedback(instance.ctx.db, track_id, action)
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        instance.ctx.emit("library", {"kind": "feedback", "track_id": track_id, "action": action})
        return {"feedback": feedback}

    @router.post("/play/youtube")
    async def post_play_youtube(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        """*"coloca um video do youtube msm, sem baixar nada"* -- this route.

        Nothing is downloaded: the device's own YouTube app plays it, so the Pi
        sends a video id and stops working. Only backends that can do it offer
        it; everything else says so instead of pretending.
        """
        url = str(body.get("url") or body.get("video_id") or "").strip()
        if not url:
            raise ApiError(400, "bad_request", "Send the YouTube URL as \"url\".")
        video_id = sources.youtube_video_id(url)
        if not video_id:
            raise ApiError(
                400, "bad_url", "Isso não parece um link de vídeo do YouTube.",
                "Paste a link like https://www.youtube.com/watch?v=... or https://youtu.be/...",
            )
        return await instance.play_youtube(video_id, str(body.get("title") or ""))

    # -- queue -------------------------------------------------------------
    @router.get("/queue")
    async def get_queue(_: Any = auth_dep) -> Dict[str, Any]:
        return {"queue": instance.queue_snapshot()}

    @router.post("/queue/reorder")
    async def post_queue_reorder(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        instance.reorder_queue(body.get("track_ids") or [])
        return {"queue": instance.queue_snapshot()}

    # -- outputs -------------------------------------------------------
    @router.get("/outputs")
    async def get_outputs(_: Any = auth_dep) -> Dict[str, Any]:
        return await instance.list_outputs()

    @router.put("/output")
    async def put_output(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        return await instance.set_output(body.get("type"), body.get("device_id"))

    @router.post("/outputs/{device_id}/pair")
    async def post_pair(device_id: str, body: Dict[str, Any] = None, _: Any = auth_dep) -> Dict[str, Any]:
        body = body or {}
        return await instance.pair_output(
            device_id, body.get("pin"), str(body.get("protocol") or "")
        )

    # -- playlists -----------------------------------------------------
    @router.get("/playlists")
    async def get_playlists(_: Any = auth_dep) -> Dict[str, Any]:
        return {"playlists": library.list_playlists(instance.ctx.db)}

    @router.post("/playlists")
    async def post_playlists(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "bad_request", "Uma playlist precisa de nome.")
        playlist = library.create_playlist(
            instance.ctx.db, name,
            description=body.get("description", ""), color=body.get("color", ""),
            shuffle=bool(body.get("shuffle", True)),
        )
        instance.ctx.emit("library", {"kind": "playlist_created", "playlist_id": playlist.get("id")})
        return {"playlist": playlist}

    @router.get("/playlists/{playlist_id}")
    async def get_playlist(playlist_id: str, _: Any = auth_dep) -> Dict[str, Any]:
        playlist = library.get_playlist(instance.ctx.db, playlist_id)
        if playlist is None:
            raise ApiError(404, "not_found", "Playlist não encontrada.")
        playlist = dict(playlist)
        playlist["tracks"] = library.playlist_tracks(instance.ctx.db, playlist_id)
        return {"playlist": playlist}

    @router.patch("/playlists/{playlist_id}")
    async def patch_playlist(playlist_id: str, body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        try:
            playlist = library.update_playlist(instance.ctx.db, playlist_id, **body)
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        if playlist is None:
            raise ApiError(404, "not_found", "Playlist não encontrada.")
        return {"playlist": playlist}

    @router.delete("/playlists/{playlist_id}")
    async def delete_playlist(playlist_id: str, _: Any = auth_dep) -> Dict[str, Any]:
        try:
            ok = library.delete_playlist(instance.ctx.db, playlist_id)
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        if not ok:
            raise ApiError(404, "not_found", "Playlist não encontrada.")
        return {"deleted": True}

    @router.post("/playlists/{playlist_id}/tracks")
    async def post_playlist_tracks(playlist_id: str, body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        try:
            added = library.add_tracks_to_playlist(instance.ctx.db, playlist_id, body.get("track_ids") or [])
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        return {"added": added}

    @router.delete("/playlists/{playlist_id}/tracks")
    async def delete_playlist_tracks(playlist_id: str, body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        removed = library.remove_tracks_from_playlist(instance.ctx.db, playlist_id, body.get("track_ids") or [])
        return {"removed": removed}

    @router.post("/playlists/{playlist_id}/reorder")
    async def post_playlist_reorder(playlist_id: str, body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        try:
            library.reorder_playlist(instance.ctx.db, playlist_id, body.get("track_ids") or [])
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        return {"playlist": library.get_playlist(instance.ctx.db, playlist_id)}

    @router.post("/playlists/{playlist_id}/play")
    async def post_playlist_play(playlist_id: str, _: Any = auth_dep) -> Dict[str, Any]:
        return await instance.play(playlist_id=playlist_id)

    # -- import ----------------------------------------------------------
    @router.post("/import/youtube")
    async def post_import_youtube(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        url = str(body.get("url") or "").strip()
        if not url:
            raise ApiError(400, "bad_request", "Send the YouTube link as \"url\".")
        if not sources.available():
            raise ApiError(503, "ytdlp_missing", sources.INSTALL_HINT)
        return await instance.start_import(url, body.get("playlist_id"), bool(body.get("as_playlist")))

    @router.get("/import/preview")
    async def get_import_preview(url: str, _: Any = auth_dep) -> Dict[str, Any]:
        if not sources.available():
            raise ApiError(503, "ytdlp_missing", sources.INSTALL_HINT)
        try:
            return sources.preview(url)
        except sources.SourceError as exc:
            raise ApiError(400, exc.code, exc.message, exc.hint) from exc

    @router.get("/import")
    async def get_imports(_: Any = auth_dep) -> Dict[str, Any]:
        return {"jobs": sources.list_jobs(instance.ctx.db)}

    @router.delete("/import/{job_id}")
    async def delete_import(job_id: str, _: Any = auth_dep) -> Dict[str, Any]:
        ok = sources.cancel_job(instance.ctx.db, job_id)
        return {"cancelled": ok}

    # -- schedule ------------------------------------------------------
    @router.get("/schedule")
    async def get_schedule(_: Any = auth_dep) -> Dict[str, Any]:
        return instance.schedule_status()

    @router.put("/schedule")
    async def put_schedule(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        try:
            schedule = scheduler.normalize_schedule(body)
        except ValueError as exc:
            raise ApiError(400, "invalid_schedule", str(exc)) from exc
        # replace(), not set(): the editor owns the whole subtree, so a window
        # deleted there must not come back from the file layer on the next read.
        instance.ctx.config.replace("schedule", schedule)
        instance.ctx.config.save()
        return instance.schedule_status()

    @router.get("/schedule/presets")
    async def get_presets(_: Any = auth_dep) -> Dict[str, Any]:
        return {"presets": scheduler.PRESETS}

    # -- compatibility / conversion --------------------------------------
    @router.get("/compat")
    async def get_compat(_: Any = auth_dep) -> Dict[str, Any]:
        return instance.compat_report()

    @router.post("/convert")
    async def post_convert(body: Dict[str, Any], _: Any = auth_dep) -> Dict[str, Any]:
        """Converte de verdade. Antes devolvia a lista de ids e nada acontecia.

        Roda em thread porque o ffmpeg é bloqueante e um flac de dez minutos num
        Pi 3 segura o laço de eventos -- e com ele todas as outras telas.
        """
        if not sources.ffmpeg_available():
            raise ApiError(409, "ffmpeg_missing", "O ffmpeg não está instalado, então não dá para converter as faixas.")
        ids = [str(i) for i in (body.get("track_ids") or []) if str(i).strip()]
        if not ids:
            raise ApiError(400, "no_tracks", "Diga quais faixas converter.")
        return await anyio.to_thread.run_sync(lambda: instance.convert_tracks(ids))

    # -- stats / history -------------------------------------------------
    @router.get("/stats")
    async def get_stats(_: Any = auth_dep) -> Dict[str, Any]:
        return instance.stats()

    @router.get("/history")
    async def get_history(limit: int = 50, _: Any = auth_dep) -> Dict[str, Any]:
        return {"history": library.history(instance.ctx.db, limit)}

    # -- media streaming ---------------------------------------------------
    # No auth.require_auth here on purpose: a Chromecast fetches this without
    # a project-os session cookie, so it is protected by its own signed,
    # expiring token (see _sign_media/_verify_media) instead.
    @router.get("/media/{track_id}")
    async def get_media(track_id: str, exp: int, t: str) -> FileResponse:
        if not _verify_media(instance.media_secret, track_id, exp, t):
            raise ApiError(403, "forbidden", "Esse link de mídia é inválido ou já venceu.")
        track = library.get_track(instance.ctx.db, track_id)
        if track is None or track.get("missing") or not track.get("path"):
            raise ApiError(404, "not_found", "Faixa não encontrada.")
        if not os.path.isfile(track["path"]):
            raise ApiError(404, "not_found", "O arquivo dessa faixa sumiu do disco.")
        return FileResponse(track["path"], media_type=content_type_for(track["path"]))

    return router


class BirdTunesApp(AppInstance):
    """Owns the player, the queue and the schedule loop for one BirdTunes install."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.log = getattr(ctx, "logger", None) or logging.getLogger("project_os.apps.birdtunes")
        self.router = build_router(self)

        self.media_secret = ""
        self._player = None  # type: Optional[Player]
        self._player_kind = ""
        self._queue = []  # type: List[Dict[str, Any]]
        self._recent_ids = []  # type: List[str]
        self._current_history_id = None  # type: Optional[int]
        self._current_playlist_id = None  # type: Optional[str]
        self._last_empty_reason = None  # type: Optional[str]
        self._scheduler = None  # type: Optional[scheduler.SchedulerLoop]
        self._session_task = None  # type: Optional[asyncio.Task]
        self._import_tasks = set()  # type: set
        self._rng = random.Random()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        library.register_schema(self.ctx.db)
        self._ensure_default_library_path()
        self._tidy_schedule()
        self.media_secret = self._load_or_create_secret()
        self._scheduler = scheduler.SchedulerLoop(
            get_schedule=lambda: self.ctx.config.get("schedule", {}) or {},
            on_should_play=self._on_schedule_play,
            on_should_stop=self._on_schedule_stop,
        )
        self._scheduler.start()

    async def stop(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
        self._cancel_session_timer()
        await self._stop_imports()
        if self._player is not None:
            try:
                await self._player.disconnect()
            except Exception:  # a misbehaving backend must not block shutdown
                self.log.exception("error disconnecting the birdtunes player during stop")

    async def _stop_imports(self) -> None:
        """Wind down downloads in flight before the box goes away.

        A download runs in a worker thread, which nothing can interrupt from the
        outside, so the honest lever is the same one the Cancel button uses:
        mark the job cancelled and let run_job notice between items. We wait a
        short while for that, then let go -- the database drains its own
        in-flight statements on close, so a straggler cannot corrupt anything.
        """
        tasks = [task for task in self._import_tasks if not task.done()]
        if not tasks:
            return
        for job in sources.list_jobs(self.ctx.db):
            if job.get("state") in ("queued", "running"):
                sources.cancel_job(self.ctx.db, job["id"])
        _finished, pending = await asyncio.wait(tasks, timeout=5.0)
        if pending:
            self.log.warning("%d birdtunes import(s) still running at shutdown", len(pending))

    def status(self) -> Dict[str, Any]:
        snapshot = self._player.snapshot() if self._player is not None else {
            "player": self._player_kind or self.ctx.config.get("output.type", "null"),
            "state": "idle", "connected": False, "device": "", "track": None,
            "track_id": "", "position": 0.0, "duration": 0.0,
            "volume": float(self.ctx.config.get("output.volume", DEFAULT_VOLUME) or DEFAULT_VOLUME),
            "can_pause": True, "error": "",
        }
        schedule_cfg = self.ctx.config.get("schedule", {}) or {}
        quiet = safety.is_quiet_hours(_now(self.ctx.config), schedule_cfg)
        snapshot.update({
            "output": self.ctx.config.get("output.type", "null"),
            "queue_len": len(self._queue),
            "quiet_hours_active": quiet,
            "next_change": scheduler.next_change(_now(self.ctx.config), schedule_cfg),
            "empty_reason": self._last_empty_reason,
        })
        # The dashboard card renders `summary` + `fields` when they are present
        # and otherwise dumps every key it finds. Without them a Simple-mode card
        # showed "Track ID -- / Position 0 / Can pause Yes", which is debugging
        # output, not a home screen. The raw keys stay in the dict: the app's own
        # panel and the tests read them.
        snapshot["summary"] = self._status_summary(snapshot, quiet)
        snapshot["fields"] = self._status_fields(snapshot)
        return snapshot

    def _status_summary(self, snapshot, quiet):
        # type: (Dict[str, Any], bool) -> str
        track = snapshot.get("track") or {}
        title = (track.get("title") if isinstance(track, dict) else "") or ""
        if snapshot.get("state") == "playing" and title:
            return "Playing %s" % title
        if snapshot.get("error"):
            return str(snapshot["error"])
        if quiet:
            return "Quiet hours -- nothing plays until they end."
        if snapshot.get("output", "null") == "null":
            return "No speaker chosen yet. Pick one in the app to start playing."
        return "Idle. Waiting for the next scheduled session."

    def _status_fields(self, snapshot):
        # type: (Dict[str, Any]) -> List[Dict[str, Any]]
        track = snapshot.get("track") or {}
        title = (track.get("title") if isinstance(track, dict) else "") or "--"
        fields = [
            {"label": "Speaker",
             "value": snapshot.get("device") or snapshot.get("name") or "Not chosen",
             "kind": "text"},
            {"label": "Now playing", "value": title, "kind": "text"},
            {"label": "Volume", "value": float(snapshot.get("volume") or 0.0) * 100.0,
             "kind": "percent"},
        ]
        if snapshot.get("queue_len"):
            fields.append({"label": "Na fila", "value": snapshot["queue_len"], "kind": "number"})
        if snapshot.get("next_change"):
            fields.append({"label": "Next change", "value": snapshot["next_change"], "kind": "relative"})
        return fields

    # -- config helpers ------------------------------------------------
    def _ensure_default_library_path(self) -> None:
        paths_cfg = self.ctx.config.get("library.paths", []) or []
        if paths_cfg:
            return
        default_dir = paths.media_dir() / "birdtunes"
        default_dir.mkdir(parents=True, exist_ok=True)
        self.ctx.config.set("library.paths", [str(default_dir)])
        self.ctx.config.save()

    def _tidy_schedule(self) -> None:
        """Clean the stored schedule once, at boot.

        Older versions appended a preset every time it was tapped, so a config
        written back then still holds "While I'm out" twice. Normalising only on
        write would leave those installs showing duplicates forever, with no
        button that fixes it.
        """
        stored = self.ctx.config.get("schedule", {}) or {}
        if not stored:
            return
        try:
            tidy = scheduler.normalize_schedule(stored)
        except ValueError as exc:
            self.log.warning("stored schedule is not readable: %s", exc)
            return
        if tidy != stored:
            self.ctx.config.replace("schedule", tidy)
            self.ctx.config.save()
            self.log.info("schedule tidied: %d window(s)", len(tidy["windows"]))

    def _load_or_create_secret(self) -> str:
        existing = self.ctx.db.kv_get("birdtunes", "media_secret")
        if existing:
            return str(existing)
        import secrets

        secret = secrets.token_hex(32)
        self.ctx.db.kv_set("birdtunes", "media_secret", secret)
        return secret

    # -- library -------------------------------------------------------
    def scan_library(self) -> Dict[str, Any]:
        result = library.scan(
            self.ctx.db,
            self.ctx.config.get("library.paths", []) or [],
            extensions=self.ctx.config.get("library.extensions") or None,
            follow_symlinks=bool(self.ctx.config.get("library.follow_symlinks", False)),
        )
        self.ctx.emit("library", {"kind": "scanned", **result})
        return result

    # -- output selection ------------------------------------------------
    async def _switch_player(self, kind: str) -> Player:
        cls = PLAYER_CLASSES.get(kind, NullPlayer)
        if self._player is not None and self._player_kind == kind:
            return self._player
        if self._player is not None:
            try:
                await self._player.disconnect()
            except Exception:
                self.log.exception("error disconnecting the previous birdtunes player")
        self._player = cls(self.ctx)
        self._player_kind = kind
        self._player.on_finished = self._on_track_finished
        return self._player

    async def list_outputs(self) -> Dict[str, Any]:
        backends = [cls.describe() for cls in PLAYER_CLASSES.values()]
        devices = []  # type: List[Dict[str, Any]]
        registry = getattr(self.ctx, "devices", None)
        if registry is not None:
            lister = getattr(registry, "list", None) or getattr(registry, "all", None)
            if callable(lister):
                try:
                    devices = list(lister() or [])
                except Exception:
                    devices = []
        return {"backends": backends, "devices": devices, "current": self.ctx.config.get("output.type", "null")}

    async def set_output(self, kind: Optional[str], device_id: Optional[str]) -> Dict[str, Any]:
        kind = str(kind or self.ctx.config.get("output.type", "null"))
        if kind not in PLAYER_CLASSES:
            raise ApiError(400, "bad_request", "Unknown output type %r." % kind)
        self.ctx.config.set("output.type", kind)
        if device_id is not None:
            self.ctx.config.set("output.device_id", device_id)
        self.ctx.config.save()
        await self._switch_player(kind)
        return {"type": kind, "device_id": self.ctx.config.get("output.device_id", "")}

    async def pair_output(
        self, device_id: str, pin: Optional[str], protocol: str = ""
    ) -> Dict[str, Any]:
        """Pair with a speaker. Defaults to the protocol we actually stream with.

        Three things were wrong here and all three were invisible from the UI:
        the device id went to the player as a bare string instead of the device
        it names, ``finish`` was called with (device_id, pin) against a method
        that takes only the pin, and pairing always used AirPlay -- the video
        protocol. Audio goes over RAOP, and on an Apple TV that is the one whose
        pairing puts a PIN on the screen; asking for the wrong one is why no PIN
        appeared.
        """
        player = await self._switch_player(self.ctx.config.get("output.type", "null"))
        begin = getattr(player, "begin_pairing", None)
        finish = getattr(player, "finish_pairing", None)
        if not callable(begin) or not callable(finish):
            raise ApiError(400, "bad_request", "Essa saída de som não aceita pareamento.")
        if pin is None:
            device = self._resolve_device(device_id)
            return await begin(device, protocol or "raop")
        return await finish(pin)

    def _resolve_device(self, device_id: str) -> Any:
        registry = getattr(self.ctx, "devices", None)
        if registry is None or not device_id:
            return {"id": device_id, "name": device_id}
        getter = getattr(registry, "get", None)
        if callable(getter):
            try:
                found = getter(device_id)
                if found is not None:
                    return found
            except Exception:
                pass
        return {"id": device_id, "name": device_id}

    # -- playback --------------------------------------------------------
    def _media_url(self, track: Dict[str, Any]) -> str:
        exp = int(time.time()) + MEDIA_URL_TTL
        token = _sign_media(self.media_secret, track["id"], exp)
        return self.ctx.app_url("/media/%s?exp=%d&t=%s" % (track["id"], exp, token))

    async def play_youtube(self, video_id: str, title: str = "") -> Dict[str, Any]:
        """Hand a YouTube video to the device. Nothing is downloaded or stored.

        Quiet hours still apply: "without downloading" changes where the sound
        comes from, not whether the bird should be hearing it at 3am.
        """
        schedule_cfg = self.ctx.config.get("schedule", {}) or {}
        can_play, reason = safety.check_can_play(_now(self.ctx.config), schedule_cfg, device_available=True)
        if not can_play:
            self._last_empty_reason = "quiet_hours"
            raise ApiError(409, "quiet_hours", reason)

        player = await self._switch_player(self.ctx.config.get("output.type", "null"))
        caster = getattr(player, "play_youtube", None)
        if not callable(caster):
            raise ApiError(
                409, "output_cannot_cast_youtube",
                "A saída %s não consegue tocar vídeo do YouTube sem baixar antes."
                % getattr(player, "name", "current"),
                "Chromecast can. For AirPlay, import the audio instead.",
            )
        if not player.connected:
            try:
                await player.connect(self._resolve_device(self.ctx.config.get("output.device_id", "")))
            except PlayerError as exc:
                raise ApiError(503, exc.code, exc.message, exc.hint) from exc
        try:
            result = await caster(video_id, title)
        except PlayerError as exc:
            raise ApiError(502, exc.code, exc.message, exc.hint or None) from exc
        self.ctx.emit("playback", {"kind": "youtube", "video_id": video_id})
        return result

    async def play(self, track_id: Optional[str] = None, playlist_id: Optional[str] = None) -> Dict[str, Any]:
        schedule_cfg = self.ctx.config.get("schedule", {}) or {}
        can_play, reason = safety.check_can_play(_now(self.ctx.config), schedule_cfg, device_available=True)
        if not can_play:
            self._last_empty_reason = "quiet_hours"
            raise ApiError(409, "quiet_hours", reason)

        kind = self.ctx.config.get("output.type", "null")
        player = await self._switch_player(kind)
        device_id = self.ctx.config.get("output.device_id", "")
        if not player.connected:
            try:
                await player.connect(self._resolve_device(device_id))
            except PlayerError as exc:
                raise ApiError(503, exc.code, exc.message, exc.hint) from exc

        if playlist_id is not None:
            self._current_playlist_id = playlist_id

        track = None  # type: Optional[Dict[str, Any]]
        if track_id:
            track = library.get_track(self.ctx.db, track_id)
            if track is None:
                raise ApiError(404, "not_found", "Faixa não encontrada.")
        else:
            track = self._pick_next(kind)

        if track is None:
            return {"playing": False, "reason": self._last_empty_reason, "message": _empty_reason_message(self._last_empty_reason)}

        await self._start_track(player, track)
        return {"playing": True, "track": track}

    def _pick_next(self, backend: str) -> Optional[Dict[str, Any]]:
        candidates, reason = library.candidate_set(self.ctx.db, self._current_playlist_id or library.ALL_PLAYLIST_ID, backend)
        self._last_empty_reason = reason
        if not candidates:
            return None
        pool = [dict(t, feedback=library.feedback_for(self.ctx.db, t["id"])) for t in candidates]
        cfg = self.ctx.config.get("recommender", {}) or {}
        picked = recommender.pick_next(pool, cfg, _now(self.ctx.config), self._recent_ids, self._rng)
        return picked

    async def _start_track(self, player: Player, track: Dict[str, Any]) -> None:
        volume = safety.clamp_volume(
            self.ctx.config.get("output.volume", DEFAULT_VOLUME), self.ctx.config.raw_dict()
        )
        await player.set_volume(volume)
        url = self._media_url(track) if player.needs_media_url or track.get("source") == "url" else ""
        if not url and not track.get("path") and track.get("source") != "url":
            self._last_empty_reason = "all_incompatible"
            return
        target = track.get("source_url") if track.get("source") == "url" else (url or track.get("path", ""))
        payload = Track(
            id=track["id"], path=track.get("path", ""), title=track.get("title", ""),
            artist=track.get("artist", ""), album=track.get("album", ""),
            duration=track.get("duration", 0.0), source=track.get("source", ""), url=target,
        )
        self._current_history_id = library.record_play_start(self.ctx.db, track["id"], device_label(self))
        self._remember_recent(track["id"])
        await player.play(payload, url=target)
        self._start_session_timer()
        self.ctx.emit("state", {"kind": "playing", "track_id": track["id"]})

    def _remember_recent(self, track_id: str) -> None:
        self._recent_ids.append(track_id)
        if len(self._recent_ids) > MAX_RECENT_IDS:
            self._recent_ids = self._recent_ids[-MAX_RECENT_IDS:]

    def _on_track_finished(self, info: Dict[str, Any]) -> None:
        if self._current_history_id is not None:
            completed = info.get("reason") == "finished"
            library.record_play_end(self.ctx.db, self._current_history_id, info.get("track_id", ""), completed, info.get("reason", ""))
            self._current_history_id = None
        self.ctx.emit("state", {"kind": info.get("reason", "stopped"), "track_id": info.get("track_id", "")})
        if info.get("reason") == "finished":
            asyncio.ensure_future(self._auto_advance())

    async def _auto_advance(self) -> None:
        schedule_cfg = self.ctx.config.get("schedule", {}) or {}
        can_play, _reason = safety.check_can_play(_now(self.ctx.config), schedule_cfg, device_available=True)
        if not can_play:
            return
        try:
            await self.play()
        except ApiError:
            pass

    async def pause(self) -> Dict[str, Any]:
        if self._player is None:
            raise ApiError(409, "not_playing", "Não tem nada tocando.")
        await self._player.pause()
        return self.status()

    async def resume(self) -> Dict[str, Any]:
        if self._player is None:
            raise ApiError(409, "not_playing", "Não tem nada tocando.")
        await self._player.resume()
        return self.status()

    async def stop_playback(self) -> Dict[str, Any]:
        self._cancel_session_timer()
        if self._player is not None:
            await self._player.stop()
        return self.status()

    async def next_track(self) -> Dict[str, Any]:
        if self._player is not None:
            await self._player.stop()
        return await self.play()

    async def set_volume(self, value: Any) -> Dict[str, Any]:
        volume = safety.clamp_volume(value, self.ctx.config.raw_dict())
        self.ctx.config.set("output.volume", volume)
        self.ctx.config.save()
        if self._player is not None:
            await self._player.set_volume(volume)
        return {"volume": volume}

    # -- session cap -----------------------------------------------------
    def _start_session_timer(self) -> None:
        self._cancel_session_timer()
        minutes = float(self.ctx.config.get("playback.max_session_minutes", 120) or 0)
        if minutes <= 0:
            return
        self._session_task = asyncio.ensure_future(self._session_cap(minutes * 60.0))

    def _cancel_session_timer(self) -> None:
        task, self._session_task = self._session_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _session_cap(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            await self.stop_playback()
        except asyncio.CancelledError:
            raise

    # -- queue (simple lookahead view, not a hard-committed list) ---------
    def queue_snapshot(self) -> List[Dict[str, Any]]:
        return list(self._queue)

    def reorder_queue(self, track_ids: List[str]) -> None:
        by_id = {t["id"]: t for t in self._queue}
        self._queue = [by_id[t] for t in track_ids if t in by_id]

    # -- schedule ----------------------------------------------------------
    def schedule_status(self) -> Dict[str, Any]:
        cfg = self.ctx.config.get("schedule", {}) or {}
        return {
            "schedule": cfg,
            "next_change": scheduler.next_change(_now(self.ctx.config), cfg),
            "active_window": scheduler.active_window(_now(self.ctx.config), cfg),
        }

    async def _on_schedule_play(self, window: Dict[str, Any]) -> None:
        playlist_id = window.get("playlist_id") or library.ALL_PLAYLIST_ID
        volume_override = window.get("volume")
        if volume_override is not None:
            self.ctx.config.set("output.volume", safety.clamp_volume(volume_override, self.ctx.config.raw_dict()))
        try:
            await self.play(playlist_id=playlist_id)
        except ApiError:
            self.log.info("scheduled window %r had nothing to play", window.get("id"))

    async def _on_schedule_stop(self) -> None:
        await self.stop_playback()

    # -- import ----------------------------------------------------------
    async def start_import(self, url: str, playlist_id: Optional[str], as_playlist: bool) -> Dict[str, Any]:
        target_playlist_id = playlist_id
        if as_playlist and not target_playlist_id:
            try:
                preview = sources.preview(url)
            except sources.SourceError as exc:
                raise ApiError(400, exc.code, exc.message, exc.hint) from exc
            created = library.create_playlist(self.ctx.db, preview.get("title") or "YouTube import", source="youtube", source_url=url)
            target_playlist_id = created["id"]
        kind = "playlist" if as_playlist or "list=" in url else "video"
        job = sources.create_job(self.ctx.db, url, kind=kind, playlist_id=target_playlist_id)
        dest_dir = str(paths.data_dir(APP_ID) / "downloads")
        quality = str(self.ctx.config.get("import.youtube.quality", "192"))
        # Kept in a set, not fired and forgotten: stop() has to be able to wind
        # these down, and a bare ensure_future is also collectable mid-flight.
        task = asyncio.ensure_future(self._run_import(job["id"], dest_dir, quality))
        self._import_tasks.add(task)
        task.add_done_callback(self._import_tasks.discard)
        return {"job_id": job["id"], "playlist_id": target_playlist_id}

    async def _run_import(self, job_id: str, dest_dir: str, quality: str) -> None:
        loop = asyncio.get_event_loop()

        def on_progress(payload: Dict[str, Any]) -> None:
            self.ctx.emit("import", payload)

        def is_cancelled() -> bool:
            job = sources.get_job(self.ctx.db, job_id)
            return job is not None and job.get("state") == "cancelled"

        try:
            result = await loop.run_in_executor(
                None, sources.run_job, self.ctx.db, job_id, dest_dir, quality, is_cancelled, on_progress
            )
        except Exception as exc:
            self.log.exception("birdtunes import job %s failed", job_id)
            result = {"id": job_id, "state": "error", "message": str(exc)}
        self.ctx.emit("import", {"kind": "finished", "job": result})

    # -- compatibility -----------------------------------------------------
    def compat_report(self) -> Dict[str, Any]:
        backend = self.ctx.config.get("output.type", "null")
        tracks = library.list_tracks(self.ctx.db, include_missing=False)
        compatible = 0
        incompatible = []  # type: List[Dict[str, Any]]
        for track in tracks:
            if library.compatible_with(track.get("container", ""), track.get("codec", ""), backend):
                compatible += 1
            else:
                incompatible.append(track)
        return {
            "output": backend,
            "total": len(tracks),
            "compatible": compatible,
            "incompatible": len(incompatible),
            "incompatible_tracks": [t["id"] for t in incompatible],
            "ffmpeg_available": sources.ffmpeg_available(),
            # Dito antes do clique: sem yt-dlp o botão de importar só pode
            # responder 503, e respondia -- depois de a pessoa colar o link.
            "ytdlp_available": sources.available(),
        }

    # -- conversão ------------------------------------------------------
    def convert_tracks(self, track_ids: List[str]) -> Dict[str, Any]:
        """Converte faixas para MP3 e devolve o que aconteceu com cada uma.

        Bloqueante de propósito -- quem chama roda isto numa thread. A faixa
        nova entra na biblioteca pelo mesmo caminho de sempre (o scan), então
        ela herda pasta, playlists por pasta e tudo o mais; a antiga fica no
        disco, porque desfazer uma conversão não é botão nenhum.
        """
        convertidas = []  # type: List[Dict[str, Any]]
        falhas = []  # type: List[Dict[str, Any]]
        for track_id in track_ids:
            track = library.get_track(self.ctx.db, track_id)
            if track is None or not track.get("path"):
                falhas.append({"id": track_id, "error": "essa faixa não está na biblioteca"})
                continue
            try:
                destino = sources.convert_to_mp3(track["path"])
            except Exception as exc:  # noqa: BLE001 -- vira mensagem, não traceback
                self.log.warning("convert failed for %s: %s", track_id, exc)
                falhas.append({"id": track_id, "error": str(exc)})
                continue
            convertidas.append({"id": track_id, "path": destino,
                                "title": track.get("title") or ""})

        if convertidas:
            # Uma varredura só no fim, com os mesmos parâmetros do scan normal.
            self.scan_library()
        return {
            "converted": convertidas,
            "failed": falhas,
            "count": len(convertidas),
        }

    # -- stats ---------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        tracks = library.list_tracks(self.ctx.db, include_missing=False)
        total_plays = 0
        total_likes = 0
        for track in tracks:
            feedback = library.feedback_for(self.ctx.db, track["id"])
            total_plays += int(feedback.get("plays", 0) or 0)
            total_likes += int(feedback.get("likes", 0) or 0)
        return {
            "tracks": len(tracks),
            "playlists": len(library.list_playlists(self.ctx.db)) - 1,
            "total_plays": total_plays,
            "total_likes": total_likes,
        }


def device_label(instance: "BirdTunesApp") -> str:
    player = instance._player
    if player is None or player.device is None:
        return ""
    from .players.base import device_label as _label

    return _label(player.device)


def _now(config: Any = None) -> Any:
    """The local time of the house, not of the card.

    The image boots on UTC -- it has no way to know where it will be plugged in
    -- so on a fresh Pi "quiet hours 20:00" meant 17:00 in Brazil. For an app
    whose whole job is *when* to make noise, that is not a cosmetic bug.
    """
    import datetime as dt

    name = ""
    if config is not None:
        try:
            name = str(config.get("system.timezone", "") or "")
        except Exception:  # pragma: no cover - config is never this broken
            name = ""
    if not name:
        return dt.datetime.now()
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python 3.9 without tzdata
        return dt.datetime.now()
    try:
        return dt.datetime.now(ZoneInfo(name)).replace(tzinfo=None)
    except Exception:
        logging.getLogger("project_os.apps.birdtunes").warning(
            "unknown timezone %r; falling back to the system clock", name)
        return dt.datetime.now()


def setup(ctx: AppContext) -> BirdTunesApp:
    return BirdTunesApp(ctx)


__all__ = ["APP_ID", "BirdTunesApp", "PLAYER_CLASSES", "build_router", "setup"]
