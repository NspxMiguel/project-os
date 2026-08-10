"""No speaker at all -- the output BirdTunes runs with out of the box.

*"nada hardcoded, tudo configuravel"* cuts both ways: the app must not force
anyone to own an Apple TV or a Chromecast just to try it. ``manifest.json``
defaults ``output.type`` to ``"null"`` precisely so a fresh install, a CI box,
or a machine with neither pyatv nor pychromecast installed still has
something to select and can exercise the whole scheduler/queue/feedback loop
end to end -- it just logs what it would have played instead of making sound.

This is also the backend the test suite drives: it needs no hardware, no
optional dependency, and its "track finished" timing is short and
deterministic (capped, and always at least a fraction of a second) so a test
can await it without a real song's worth of sleep.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    PlaybackState,
    Player,
    PlayerError,
    REASON_FINISHED,
    REASON_STOPPED,
    Track,
    clamp_volume,
)

#: A track never "plays" for longer than this in the null backend, even if
#: its reported duration is long -- nobody is listening, so there is no
#: reason to make a test (or a dry run) wait for it.
MAX_SIMULATED_SECONDS = 2.0
#: Never zero either: callers that structure around "a track played for a
#: moment" need that moment to actually elapse.
MIN_SIMULATED_SECONDS = 0.05


class NullPlayer(Player):
    """Logs playback instead of producing sound. Always available."""

    name = "Nenhuma (só registra)"
    kind = "null"
    device_kinds = []  # type: List[str]
    can_pause = True
    needs_media_url = False

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._connected = False
        self._timer = None  # type: Optional[asyncio.Future]

    @classmethod
    def available(cls) -> Tuple[bool, str]:
        return True, ""

    async def connect(self, device: Any) -> None:
        await self.disconnect()
        self._set_state(PlaybackState.CONNECTING)
        self.device = device
        self._connected = True
        self._set_state(PlaybackState.IDLE)
        self.log.info("null player \"connected\" (no device involved)")

    @property
    def connected(self) -> bool:
        return self._connected

    async def play(self, track: Any, url: str = "") -> None:
        if not self._connected:
            raise PlayerError(
                "Not connected.", code="not_connected",
                hint="Escolha uma saída de som nas configurações do BirdTunes (a saída vazia não liga em nada).",
            )
        item = Track.coerce(track)
        await self._cancel_timer()
        self._begin_playback(item)
        self.log.info("null player: would play %s (%s)", item.label, url or item.path)
        seconds = min(MAX_SIMULATED_SECONDS, max(MIN_SIMULATED_SECONDS, item.duration or MIN_SIMULATED_SECONDS))
        self._timer = asyncio.ensure_future(self._auto_finish(seconds))

    async def _auto_finish(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            self._finish_playback(REASON_FINISHED)
        except asyncio.CancelledError:
            raise

    async def _cancel_timer(self) -> None:
        task, self._timer = self._timer, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        await self._cancel_timer()
        self._finish_playback(REASON_STOPPED)
        self._set_state(PlaybackState.STOPPED)

    async def pause(self) -> None:
        await self._cancel_timer()
        self._mark_paused()

    async def resume(self) -> None:
        if self.track is not None:
            remaining = max(MIN_SIMULATED_SECONDS, (self.track.duration or MIN_SIMULATED_SECONDS) - self.position)
            self._timer = asyncio.ensure_future(self._auto_finish(min(MAX_SIMULATED_SECONDS, remaining)))
        self._mark_resumed()

    async def set_volume(self, level: float) -> None:
        self.volume = clamp_volume(level, self.volume)

    async def disconnect(self) -> None:
        await self._cancel_timer()
        self._connected = False
        self._set_state(PlaybackState.IDLE)

    async def discover(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        return [{"id": "null", "name": "No output (log only)", "address": "", "kind": "null"}]


__all__ = ["MAX_SIMULATED_SECONDS", "MIN_SIMULATED_SECONDS", "NullPlayer"]
