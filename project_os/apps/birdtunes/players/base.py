"""What every BirdTunes output backend has in common.

A *player* owns one speaker. It is handed a device (whatever
``DeviceRegistry`` stored, as a plain dict), a track and a LAN URL for that
track, and it is expected to make sound come out -- or to explain, in a
sentence a human can act on, why it cannot.

Two rules shape this module:

* **Players never import the library.** They receive a duck-typed track
  (:class:`Track`, or any dict/object with the same fields), which keeps
  ``library.py`` and ``players/`` free of an import cycle.
* **A dead speaker is not a crash.** Unplugged Apple TVs, Chromecasts that
  wandered onto the guest VLAN and Home Assistant restarts are ordinary
  weather. Every backend catches them, lands in
  :attr:`PlaybackState.ERROR` with a readable message, and lets the
  scheduler retry with backoff.

Track completion is reported two ways, because the scheduler wants a push
and tests want a pull: set :attr:`Player.on_finished` for a callback, or
``await player.wait_finished()`` for the future.
"""

from __future__ import annotations

import abc
import asyncio
import enum
import inspect
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Extension -> Content-Type. Cast receivers and Home Assistant both refuse
#: to guess, and ``application/octet-stream`` makes a Chromecast sit silent.
CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".wma": "audio/x-ms-wma",
    ".mka": "audio/x-matroska",
}

DEFAULT_CONTENT_TYPE = "audio/mpeg"

#: Why a track stopped. The scheduler advances the queue on ``finished``,
#: leaves it alone on ``stopped`` and backs off on ``error``.
REASON_FINISHED = "finished"
REASON_STOPPED = "stopped"
REASON_ERROR = "error"
REASON_REPLACED = "replaced"


class PlayerError(Exception):
    """A backend could not do what was asked, and the user should know why.

    ``hint`` is an actionable next step ("install pyatv", "pair the Apple TV
    first") and is safe to show in the UI verbatim.
    """

    def __init__(
        self,
        message: str,
        code: str = "player_error",
        hint: str = "",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.retryable = retryable

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class PlaybackState(str, enum.Enum):
    """Where a player is right now. Values are what the API sends."""

    IDLE = "idle"
    CONNECTING = "connecting"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Track(object):
    """The slice of a library track a player actually needs.

    Deliberately not the library's row type: players must stay importable
    without the library, and the library must stay free to change its own
    representation.
    """

    __slots__ = (
        "id",
        "path",
        "title",
        "artist",
        "album",
        "duration",
        "source",
        "url",
    )

    def __init__(
        self,
        id: str = "",
        path: str = "",
        title: str = "",
        artist: str = "",
        album: str = "",
        duration: float = 0.0,
        source: str = "",
        url: str = "",
    ) -> None:
        self.id = str(id or "")
        self.path = str(path or "")
        self.title = str(title or "")
        self.artist = str(artist or "")
        self.album = str(album or "")
        try:
            self.duration = float(duration or 0.0)
        except (TypeError, ValueError):
            self.duration = 0.0
        self.source = str(source or "")
        self.url = str(url or "")

    @classmethod
    def coerce(cls, value: Any) -> "Track":
        """Accept a Track, a mapping or any object with the right attributes."""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, dict):
            getter = value.get  # type: Callable[..., Any]
        else:
            def getter(key, default=None):  # type: ignore[misc]
                return getattr(value, key, default)
        return cls(
            id=getter("id", "") or "",
            path=getter("path", "") or "",
            title=getter("title", "") or "",
            artist=getter("artist", "") or "",
            album=getter("album", "") or "",
            duration=getter("duration", 0.0) or 0.0,
            source=getter("source", "") or "",
            url=getter("url", "") or "",
        )

    @property
    def label(self) -> str:
        """Something worth putting on a screen, never empty."""
        if self.title and self.artist:
            return "%s - %s" % (self.artist, self.title)
        if self.title:
            return self.title
        if self.path:
            return os.path.basename(self.path)
        return self.id or "track"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration": self.duration,
            "source": self.source,
            "url": self.url,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Track(%r, %r)" % (self.id, self.label)


# ---------------------------------------------------------------------------
# helpers shared by the backends
# ---------------------------------------------------------------------------
def is_url(value: str) -> bool:
    text = str(value or "").lower()
    return text.startswith("http://") or text.startswith("https://")


def content_type_for(target: str) -> str:
    """Content-Type for a path or URL, by extension."""
    text = str(target or "")
    for separator in ("?", "#"):
        cut = text.find(separator)
        if cut != -1:
            text = text[:cut]
    _, ext = os.path.splitext(text)
    return CONTENT_TYPES.get(ext.lower(), DEFAULT_CONTENT_TYPE)


def clamp_volume(level: Any, default: float = 0.35) -> float:
    """Volume as a 0.0-1.0 float. Nonsense becomes ``default``, not a crash."""
    try:
        value = float(level)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def device_field(device: Any, *names: str) -> str:
    """First non-empty field out of a device dict or object."""
    if device is None:
        return ""
    for name in names:
        if isinstance(device, dict):
            value = device.get(name)
        else:
            value = getattr(device, name, None)
        if value:
            return str(value)
    return ""


def device_properties(device: Any) -> Dict[str, Any]:
    if isinstance(device, dict):
        props = device.get("properties")
    else:
        props = getattr(device, "properties", None)
    return props if isinstance(props, dict) else {}


def device_label(device: Any) -> str:
    # Empty, not the word "device": this string is shown to a person ("Speaker:
    # device" was on the dashboard), and callers already have a better fallback
    # than a placeholder -- the player's own name.
    return device_field(device, "display_name", "custom_name", "name", "id") or ""


# ---------------------------------------------------------------------------
# the abstract player
# ---------------------------------------------------------------------------
class Player(abc.ABC):
    """One speaker, one queue slot at a time.

    Subclasses implement the seven verbs below plus :meth:`available`. The
    bookkeeping every backend would otherwise duplicate -- state, the
    completion future, volume clamping, status -- lives here.
    """

    #: Human name for the UI.
    name = "Player"
    #: Stable identifier used in config (``output.type``) and ``get_player``.
    kind = "base"
    #: Device kinds from ``DeviceRegistry`` this backend can drive.
    device_kinds = []  # type: List[str]
    #: False when the transport genuinely cannot pause (RAOP streaming).
    can_pause = True
    #: True when :meth:`play` needs a LAN-reachable URL rather than a path.
    needs_media_url = False

    def __init__(self, ctx: Any = None) -> None:
        self.ctx = ctx
        self.log = getattr(ctx, "logger", None) or logging.getLogger(
            "project_os.app.birdtunes.player.%s" % self.kind
        )
        self.device = None  # type: Any
        self.track = None  # type: Optional[Track]
        self.last_error = ""
        self.volume = clamp_volume(self._config_get("output.volume", 0.35))
        #: ``callback(info: dict)`` fired once per track when playback ends.
        self.on_finished = None  # type: Optional[Callable[[Dict[str, Any]], Any]]
        #: ``callback(state: str, detail: dict)`` fired on every transition.
        self.on_state_change = None  # type: Optional[Callable[[str, Dict[str, Any]], Any]]
        self._state = PlaybackState.IDLE
        self._finished = None  # type: Optional[asyncio.Future]
        self._started_at = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0

    # -- configuration ---------------------------------------------------
    def _config_get(self, path: str, default: Any = None) -> Any:
        config = getattr(self.ctx, "config", None)
        if config is None:
            return default
        try:
            value = config.get(path, default)
        except Exception:  # pragma: no cover - a broken config must not kill audio
            return default
        return default if value is None else value

    @property
    def max_volume(self) -> float:
        return clamp_volume(self._config_get("output.max_volume", 0.6), 0.6)

    # -- availability ----------------------------------------------------
    @classmethod
    @abc.abstractmethod
    def available(cls) -> Tuple[bool, str]:
        """``(usable_here, hint)``.

        The hint is shown when the answer is no, so it must name the fix
        ("pip install pyatv"), never merely restate the problem.
        """

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        """The row ``/outputs`` renders for this backend."""
        try:
            ok, hint = cls.available()
        except Exception as exc:  # pragma: no cover - probing must never raise
            ok, hint = False, "could not probe %s: %s" % (cls.kind, exc)
        return {
            "kind": cls.kind,
            "name": cls.name,
            "available": bool(ok),
            "hint": hint or "",
            "device_kinds": list(cls.device_kinds),
        }

    # -- state -----------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state.value

    @property
    def playback_state(self) -> PlaybackState:
        return self._state

    @property
    def connected(self) -> bool:
        """Overridden by backends that hold a live connection."""
        return self._state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
            PlaybackState.CONNECTING,
        )

    def _set_state(self, state: PlaybackState, error: str = "") -> None:
        if error:
            self.last_error = error
        elif state != PlaybackState.ERROR:
            self.last_error = ""
        if state == self._state and not error:
            return
        self._state = state
        detail = {
            "player": self.kind,
            "device": device_label(self.device) if self.device is not None else "",
            "track_id": self.track.id if self.track else "",
            "error": self.last_error,
        }
        self._fire(self.on_state_change, state.value, detail)

    def _fail(self, message: str, exc: Optional[BaseException] = None) -> str:
        """Record an error, log it once, return the human message."""
        text = message
        if exc is not None:
            detail = str(exc).strip()
            text = "%s: %s" % (message, detail) if detail else message
            self.log.warning("%s player: %s", self.kind, text, exc_info=False)
            self.log.debug("%s player traceback", self.kind, exc_info=True)
        else:
            self.log.warning("%s player: %s", self.kind, text)
        self._set_state(PlaybackState.ERROR, text)
        return text

    def _fire(self, callback: Optional[Callable[..., Any]], *args: Any) -> None:
        """Call a user callback without ever letting it break playback."""
        if callback is None:
            return
        try:
            result = callback(*args)
        except Exception:  # pragma: no cover - listener bugs are not ours
            self.log.warning("%s player callback failed", self.kind, exc_info=True)
            return
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_event_loop()
                task = loop.create_task(result)  # type: ignore[arg-type]
            except RuntimeError:  # pragma: no cover - no loop, nothing to await
                return
            task.add_done_callback(self._swallow_task_result)

    def _swallow_task_result(self, task: "asyncio.Future") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - listener bugs are not ours
            self.log.warning("%s player callback raised: %s", self.kind, exc)

    # -- playback bookkeeping -------------------------------------------
    def _begin_playback(self, track: Track) -> "asyncio.Future":
        """Arm the completion future for a new track.

        A track already in flight is resolved as ``replaced`` first, so a
        caller waiting on the previous future is never left hanging.
        """
        if self._finished is not None and not self._finished.done():
            self._finish_playback(REASON_REPLACED)
        self.track = track
        self._started_at = time.monotonic()
        self._paused_at = 0.0
        self._paused_total = 0.0
        loop = asyncio.get_event_loop()
        self._finished = loop.create_future()
        self._set_state(PlaybackState.PLAYING)
        return self._finished

    def _finish_playback(
        self,
        reason: str = REASON_FINISHED,
        error: str = "",
    ) -> None:
        """Resolve the current track exactly once and tell the scheduler."""
        future, self._finished = self._finished, None
        if future is None:
            return
        track = self.track
        info = {
            "player": self.kind,
            "track_id": track.id if track else "",
            "track": track.as_dict() if track else None,
            "reason": reason,
            "error": error,
            "elapsed": self.position,
            "device": device_label(self.device) if self.device is not None else "",
        }
        if not future.done():
            future.set_result(info)
        if reason == REASON_FINISHED:
            self._set_state(PlaybackState.IDLE)
        elif reason == REASON_ERROR:
            self._set_state(PlaybackState.ERROR, error or "playback failed")
        elif reason == REASON_STOPPED:
            self._set_state(PlaybackState.STOPPED)
        if reason != REASON_REPLACED:
            self._fire(self.on_finished, info)

    async def wait_finished(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Block until the current track ends.

        Returns the same dict :attr:`on_finished` receives; returns a
        synthetic ``stopped`` record when nothing is playing.
        """
        future = self._finished
        if future is None:
            return {
                "player": self.kind,
                "track_id": self.track.id if self.track else "",
                "track": self.track.as_dict() if self.track else None,
                "reason": REASON_STOPPED,
                "error": self.last_error,
                "elapsed": self.position,
                "device": device_label(self.device) if self.device is not None else "",
            }
        if timeout is None:
            return await future
        return await asyncio.wait_for(asyncio.shield(future), timeout)

    @property
    def position(self) -> float:
        """Seconds into the current track, best effort."""
        if not self._started_at:
            return 0.0
        if self._paused_at:
            return max(0.0, self._paused_at - self._started_at - self._paused_total)
        return max(0.0, time.monotonic() - self._started_at - self._paused_total)

    def _mark_paused(self) -> None:
        if not self._paused_at:
            self._paused_at = time.monotonic()
        self._set_state(PlaybackState.PAUSED)

    def _mark_resumed(self) -> None:
        if self._paused_at:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = 0.0
        self._set_state(PlaybackState.PLAYING)

    # -- the verbs -------------------------------------------------------
    @abc.abstractmethod
    async def connect(self, device: Any) -> None:
        """Attach to a speaker. Raises :class:`PlayerError` when it cannot."""

    @abc.abstractmethod
    async def play(self, track: Any, url: str = "") -> None:
        """Start ``track``. ``url`` is the signed LAN media URL for it."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop playback. Safe to call when nothing is playing."""

    @abc.abstractmethod
    async def pause(self) -> None:
        """Pause playback."""

    @abc.abstractmethod
    async def resume(self) -> None:
        """Resume after :meth:`pause`."""

    @abc.abstractmethod
    async def set_volume(self, level: float) -> None:
        """Set output volume, 0.0-1.0."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Release the speaker. Safe to call twice, must never raise."""

    # -- optional extras -------------------------------------------------
    async def discover(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Speakers this backend can see right now (for ``GET /outputs``)."""
        return []

    async def status(self) -> Dict[str, Any]:
        """Snapshot for the API. Backends extend, they do not replace."""
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        """The synchronous half of :meth:`status`, safe to call anywhere."""
        track = self.track
        return {
            "player": self.kind,
            "name": self.name,
            "state": self._state.value,
            "connected": bool(self.connected),
            "device": device_label(self.device) if self.device is not None else "",
            "device_id": device_field(self.device, "id", "identifier", "uuid"),
            "track": track.as_dict() if track else None,
            "track_id": track.id if track else "",
            "position": round(self.position, 2),
            "duration": round(track.duration, 2) if track else 0.0,
            "volume": round(self.volume, 3),
            "can_pause": bool(self.can_pause),
            "error": self.last_error,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "%s(state=%r, device=%r)" % (
            type(self).__name__,
            self._state.value,
            device_label(self.device) if self.device is not None else None,
        )


__all__ = [
    "CONTENT_TYPES",
    "DEFAULT_CONTENT_TYPE",
    "REASON_ERROR",
    "REASON_FINISHED",
    "REASON_REPLACED",
    "REASON_STOPPED",
    "PlaybackState",
    "Player",
    "PlayerError",
    "Track",
    "clamp_volume",
    "content_type_for",
    "device_field",
    "device_label",
    "device_properties",
    "is_url",
]
