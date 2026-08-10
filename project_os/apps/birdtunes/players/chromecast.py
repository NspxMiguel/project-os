"""Chromecast output through PyChromecast 13.x -- the *"chromecast (configuravel)"* half.

PyChromecast is a synchronous library, so every call into it runs inside
``run_in_executor``; nothing here blocks the event loop. Unlike AirPlay,
Chromecast has no call that blocks for the length of the track, so end of
track is detected by polling ``media_controller.status`` -- the closest thing
to a push notification pychromecast offers without the caller registering its
own listener thread.

Chromecast also cannot open a local file itself (:attr:`needs_media_url` is
True): it fetches from a URL, so the caller must pass the app's own signed
``/media/<id>`` link, not a filesystem path.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    PlaybackState,
    Player,
    PlayerError,
    REASON_ERROR,
    REASON_FINISHED,
    REASON_STOPPED,
    Track,
    clamp_volume,
    content_type_for,
    device_field,
    device_label,
)

INSTALL_HINT = "pip install pychromecast (Chromecast support is an optional dependency)"

DEVICE_KINDS = ["chromecast", "google_cast", "chromecast_audio", "google_home", "nest_audio"]

CONNECT_TIMEOUT = 20.0
#: How often to poll media status for end-of-track, in seconds.
POLL_INTERVAL = 1.0


def _pychromecast() -> Any:
    try:
        import pychromecast
        import pychromecast.discovery  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the box
        raise PlayerError(
            "pychromecast is not installed, so Chromecast output is unavailable.",
            code="pychromecast_missing",
            hint=INSTALL_HINT,
            retryable=False,
        ) from exc
    return pychromecast


class ChromecastPlayer(Player):
    """Streams a signed media URL to a Cast receiver and polls for the end."""

    name = "Chromecast"
    kind = "chromecast"
    device_kinds = DEVICE_KINDS
    can_pause = True
    needs_media_url = True

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._cast = None  # type: Any
        self._watch_task = None  # type: Optional[asyncio.Future]
        self._youtube = None  # type: Any  # the device's own YouTube app, when used

    # -- availability ------------------------------------------------------
    @classmethod
    def available(cls) -> Tuple[bool, str]:
        import importlib.util

        try:
            spec = importlib.util.find_spec("pychromecast")
        except (ImportError, ValueError):  # pragma: no cover - broken install
            spec = None
        if spec is None:
            return False, INSTALL_HINT
        return True, ""

    # -- connection ----------------------------------------------------
    async def connect(self, device: Any) -> None:
        pychromecast = _pychromecast()
        await self.disconnect()
        self._set_state(PlaybackState.CONNECTING)
        self.device = device
        loop = asyncio.get_event_loop()

        host = device_field(device, "address", "host", "ip")
        if not host:
            message = self._fail("Chromecast %s has no known address." % device_label(device))
            raise PlayerError(message, code="chromecast_no_address", hint="Re-run device discovery.")
        try:
            port = int(device_field(device, "port") or 8009)
        except (TypeError, ValueError):
            port = 8009
        uuid_value = device_field(device, "id", "uuid", "identifier") or host

        try:
            # get_chromecast_from_host, not a hand-built CastInfo: the CastInfo
            # this used to make had an empty `services` set, and the socket
            # client has nothing to dial with -- it waits forever and the only
            # symptom is "timed out" against a device that answers a TCP connect
            # on 8009 immediately. The helper fills in the host service for us.
            # It also decides cast_type from the model, which matters: a TV is
            # not the "audio" this file used to hardcode.
            model = device_field(device, "model") or ""
            cast = await loop.run_in_executor(
                None,
                lambda: pychromecast.get_chromecast_from_host(
                    (host, port, uuid_value, model, device_label(device)),
                    tries=1,
                    timeout=CONNECT_TIMEOUT,
                    retry_wait=1,
                ),
            )
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: cast.wait(timeout=CONNECT_TIMEOUT)),
                CONNECT_TIMEOUT + 5.0,
            )
        except PlayerError:
            raise
        except asyncio.TimeoutError as exc:
            message = self._fail("Timed out connecting to %s" % device_label(device))
            raise PlayerError(
                message, code="chromecast_timeout",
                hint="O aparelho pode estar dormindo; tente de novo ou acorde ele pelo app Google Home.",
            ) from exc
        except asyncio.CancelledError:
            self._set_state(PlaybackState.IDLE)
            raise
        except Exception as exc:
            message = self._fail("Could not connect to %s" % device_label(device), exc)
            raise PlayerError(message, code="chromecast_connect_failed") from exc

        self._cast = cast
        self._set_state(PlaybackState.IDLE)
        self.log.info("Chromecast connected to %s", device_label(device))

    @property
    def connected(self) -> bool:
        return self._cast is not None

    # -- playback --------------------------------------------------------
    async def play(self, track: Any, url: str = "") -> None:
        if self._cast is None:
            raise PlayerError(
                "Not connected to a Chromecast.",
                code="not_connected",
                hint="Pick an output device in BirdTunes settings.",
            )
        item = Track.coerce(track)
        target = url or item.url
        if not target:
            raise PlayerError(
                "Track %s has no LAN-reachable URL." % item.label,
                code="track_unplayable",
                hint="O Chromecast precisa do endereço de mídia assinado do app, não de um caminho local.",
            )
        content_type = content_type_for(target)
        loop = asyncio.get_event_loop()
        await self._cancel_watch()
        self._begin_playback(item)
        mc = self._cast.media_controller
        try:
            await loop.run_in_executor(None, self._take_over_receiver)
            await loop.run_in_executor(
                None,
                lambda: mc.play_media(
                    target, content_type, title=item.title or item.label, stream_type="BUFFERED"
                ),
            )
            await loop.run_in_executor(None, mc.block_until_active)
        except Exception as exc:
            self.log.warning("Chromecast playback failed: %s", exc)
            self._finish_playback(REASON_ERROR, "Chromecast playback failed: %s" % exc)
            return

        # Ask the device what actually happened. play_media returning is not the
        # same as the television playing anything: with another app in front,
        # the request lands on *its* media session and the only visible effect is
        # the volume moving. Claiming "playing" then is a lie the screen repeats.
        started, detail = await loop.run_in_executor(None, self._confirm_started)
        if not started:
            self.log.warning("Chromecast did not start playing: %s", detail)
            self._finish_playback(REASON_ERROR, detail)
            raise PlayerError(
                "%s did not start playing." % device_label(self.device),
                code="chromecast_did_not_start",
                hint=detail,
            )
        self.log.info("Chromecast playing: %s", item.label)
        self._watch_task = asyncio.ensure_future(self._watch(mc))

    #: The Default Media Receiver -- the small player Google ships for exactly
    #: this, "cast a URL at me".
    DEFAULT_RECEIVER = "CC1AD845"

    async def play_youtube(self, video_id: str, title: str = "") -> Dict[str, Any]:
        """Play a YouTube video on the device itself, downloading nothing.

            "coloca um video do youtube msm, sem baixar nada"

        This is a different thing from importing: the television's own YouTube
        app plays it, so no file is fetched, stored or transcoded here, and the
        Pi is doing nothing but sending a video id. It also sidesteps the whole
        yt-dlp question -- there is no download to be blocked.
        """
        if self._cast is None:
            raise PlayerError(
                "Not connected to a Chromecast.", code="not_connected",
                hint="Pick an output device in BirdTunes settings.",
            )
        try:
            from pychromecast.controllers.youtube import YouTubeController
        except ImportError as exc:
            raise PlayerError(
                "Casting YouTube needs the casttube package.",
                code="youtube_controller_missing",
                hint="pip install casttube",
            ) from exc

        loop = asyncio.get_event_loop()
        await self._cancel_watch()
        controller = self._youtube
        if controller is None:
            controller = YouTubeController()
            self._cast.register_handler(controller)
            self._youtube = controller

        def start() -> None:
            controller.play_video(video_id)

        try:
            await asyncio.wait_for(loop.run_in_executor(None, start), CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise PlayerError(
                "Timed out asking %s to play the video." % device_label(self.device),
                code="youtube_timeout",
                hint="O aparelho pode estar dormindo.",
            ) from exc
        except Exception as exc:
            raise PlayerError(
                "Could not start the YouTube video on %s." % device_label(self.device),
                code="youtube_failed", hint=str(exc),
            ) from exc

        self._begin_playback(Track(
            id="youtube:%s" % video_id,
            title=title or ("YouTube %s" % video_id),
            url="https://www.youtube.com/watch?v=%s" % video_id,
            duration=0.0,
        ))
        self.log.info("Chromecast playing YouTube video %s", video_id)
        return {"video_id": video_id, "device": device_label(self.device)}

    def _take_over_receiver(self) -> None:
        """Put our own receiver in front before sending it a track.

        Whatever is already on the television (YouTube, Netflix, the backdrop)
        owns the media session, and play_media aimed at that session does
        nothing useful -- observed on a TCL: the video kept playing and only the
        volume moved. Quitting the other app first is what "cast to the TV"
        means everywhere else, so it is what happens here.
        """
        import time

        cast = self._cast
        if cast is None:
            return
        current = getattr(cast.status, "app_id", None) if cast.status else None
        if current == self.DEFAULT_RECEIVER:
            return
        if current:
            self.log.info("Chromecast: taking the screen from %s", cast.status.display_name)
        # start_app, not quit_app: quitting hands the screen back to whatever the
        # television shows when idle, and play_media on its own does not reliably
        # launch anything -- measured on a TCL, where the YouTube app stayed in
        # front and the track never loaded. Asking for the receiver by id is the
        # only step that actually put it on screen.
        try:
            cast.start_app(self.DEFAULT_RECEIVER)
        except Exception as exc:  # pragma: no cover - device specific
            self.log.debug("start_app failed, trying play_media anyway: %s", exc)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            app_id = getattr(cast.status, "app_id", None) if cast.status else None
            if app_id == self.DEFAULT_RECEIVER:
                return
            time.sleep(0.4)
        self.log.warning("Chromecast: the media receiver did not come up in time")

    def _confirm_started(self, timeout: float = 12.0) -> Tuple[bool, str]:
        """Whether the device really started, and what it said if it did not."""
        import time

        cast = self._cast
        if cast is None:
            return False, "The connection dropped."
        media = cast.media_controller
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            try:
                media.update_status()
            except Exception:  # pragma: no cover - transient
                pass
            status = media.status
            state = getattr(status, "player_state", "") or ""
            if state in ("PLAYING", "BUFFERING"):
                return True, state
            if state == "IDLE" and getattr(status, "idle_reason", None) == "ERROR":
                return False, (
                    "The device refused the file. Chromecast may not support this format; "
                    "converting to MP3 usually fixes it."
                )
            time.sleep(0.5)
        app = getattr(cast.status, "display_name", "") if cast.status else ""
        if app and app != "Default Media Receiver":
            return False, "%s is still on screen; it kept the media session." % app
        return False, "The device never reported playing (last state: %s)." % (state or "unknown")

    async def _watch(self, media_controller: Any) -> None:
        """Poll for the receiver reaching IDLE/FINISHED -- pychromecast has no future to await."""
        loop = asyncio.get_event_loop()
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if self._finished is None or self._finished.done():
                    return
                try:
                    state = await loop.run_in_executor(None, lambda: media_controller.status.player_state)
                    idle_reason = await loop.run_in_executor(
                        None, lambda: media_controller.status.idle_reason
                    )
                except Exception:  # pragma: no cover - device vanished mid-poll
                    return
                if state == "IDLE" and idle_reason == "FINISHED":
                    self._finish_playback(REASON_FINISHED)
                    return
        except asyncio.CancelledError:
            raise

    async def _cancel_watch(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        await self._cancel_watch()
        if self._cast is not None:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._cast.media_controller.stop)
            except Exception as exc:  # pragma: no cover - device already gone
                self.log.debug("chromecast stop failed: %s", exc)
        self._finish_playback(REASON_STOPPED)
        self._set_state(PlaybackState.STOPPED)

    async def pause(self) -> None:
        if self._cast is None:
            raise PlayerError("Not connected to a Chromecast.", code="not_connected")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._cast.media_controller.pause)
        except Exception as exc:
            raise PlayerError(
                "Could not pause %s" % device_label(self.device), code="chromecast_pause_failed"
            ) from exc
        self._mark_paused()

    async def resume(self) -> None:
        if self._cast is None:
            raise PlayerError("Not connected to a Chromecast.", code="not_connected")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._cast.media_controller.play)
        except Exception as exc:
            raise PlayerError(
                "Could not resume %s" % device_label(self.device), code="chromecast_resume_failed"
            ) from exc
        self._mark_resumed()

    async def set_volume(self, level: float) -> None:
        value = clamp_volume(level, self.volume)
        self.volume = value
        if self._cast is None:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._cast.set_volume, value)
        except Exception as exc:
            raise PlayerError(
                "Could not set the volume on %s" % device_label(self.device),
                code="chromecast_volume_failed",
            ) from exc

    async def disconnect(self) -> None:
        await self._cancel_watch()
        cast, self._cast = self._cast, None
        if cast is not None:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, cast.disconnect)
            except Exception as exc:  # pragma: no cover - already unreachable
                self.log.debug("chromecast disconnect failed: %s", exc)
        self._set_state(PlaybackState.IDLE)

    # -- discovery -------------------------------------------------------
    async def discover(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        pychromecast = _pychromecast()
        loop = asyncio.get_event_loop()
        try:
            casts, browser = await loop.run_in_executor(
                None, lambda: pychromecast.get_chromecasts(timeout=timeout)
            )
        except Exception:
            return []
        try:
            browser.stop_discovery()
        except Exception:  # pragma: no cover - best effort teardown
            pass
        out = []  # type: List[Dict[str, Any]]
        for cast in casts:
            info = getattr(cast, "cast_info", cast)
            out.append(
                {
                    "id": str(getattr(info, "uuid", "") or ""),
                    "name": str(getattr(info, "friendly_name", "") or ""),
                    "address": str(getattr(info, "host", "") or ""),
                    "port": int(getattr(info, "port", 8009) or 8009),
                    "model": str(getattr(info, "model_name", "") or ""),
                    "kind": "chromecast",
                }
            )
        return out


__all__ = ["ChromecastPlayer", "DEVICE_KINDS", "INSTALL_HINT"]
