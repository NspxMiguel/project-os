"""The schedule: *"horarios q vc n ta em casa ... horarios q vc qr q ele toque"*.

Windows are pure data (start/end/days/playlist), evaluated by pure functions
so they are unit-testable without an event loop. :class:`SchedulerLoop` is the
thin asyncio wrapper that turns "is a window active right now" into actual
play/stop calls -- it holds no scheduling logic of its own, only a timer and
two callbacks, so a bug in "which window wins" is always reproducible with a
plain function call in a test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import safety

log = logging.getLogger(__name__)

_DEFAULT_TIME = "00:00"
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: One-click presets the UI offers; they only prefill the editor (section 5).
PRESETS = [
    {
        "id": "while_out",
        "name": "While I'm out",
        "start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4],
    },
    {
        "id": "mornings",
        "name": "Mornings",
        "start": "07:00", "end": "10:00", "days": [0, 1, 2, 3, 4, 5, 6],
    },
    {
        "id": "afternoons",
        "name": "Afternoons",
        "start": "14:00", "end": "18:00", "days": [0, 1, 2, 3, 4, 5, 6],
    },
    {
        "id": "weekends",
        "name": "Weekends only",
        "start": "10:00", "end": "16:00", "days": [5, 6],
    },
]


def _parse_hhmm(value: Any, default: str) -> dt.time:
    text = str(value or default)
    try:
        hour, minute = text.split(":", 1)
        return dt.time(int(hour) % 24, int(minute) % 60)
    except (ValueError, TypeError):
        hour, minute = default.split(":", 1)
        return dt.time(int(hour), int(minute))


#: Accepted spellings for a weekday, so a schedule written by hand in
#: config.yaml ("mon") means the same thing as one written by the UI (0).
_DAY_ALIASES = {}  # type: Dict[str, int]
for _index, _name in enumerate(_WEEKDAY_NAMES):
    _DAY_ALIASES[_name.lower()] = _index
    _DAY_ALIASES[_name.lower()[:3]] = _index
_DAY_ALIASES.update({
    "seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6,
})


def _coerce_day(value: Any) -> int:
    """A weekday as 0..6, or ValueError naming what was wrong."""
    if isinstance(value, bool):  # bool is an int in Python; never a weekday
        raise ValueError("days: %r is not a weekday" % (value,))
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError("days: %d is out of range (0 = Monday .. 6 = Sunday)" % value)
    text = str(value).strip().lower()
    if text.isdigit():
        return _coerce_day(int(text))
    if text in _DAY_ALIASES:
        return _DAY_ALIASES[text]
    raise ValueError("days: %r is not a weekday" % (value,))


def _coerce_hhmm(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":", 1)
        parsed = dt.time(int(hour), int(minute))
    except (ValueError, TypeError):
        raise ValueError("%s: %r is not a time of day (expected HH:MM)" % (field, value))
    return "%02d:%02d" % (parsed.hour, parsed.minute)


_SCHEDULE_KEYS = ("enabled", "quiet_hours", "windows")
_WINDOW_KEYS = ("id", "name", "enabled", "days", "start", "end", "playlist_id", "volume")


def normalize_schedule(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a schedule coming off the wire, or raise ValueError.

    The endpoint used to write whatever it was handed straight into config, so
    ``{"schedule": {...}}`` (one wrapper too many) stored a nested dict, answered
    200, and changed nothing -- and ``days: ["mon"]`` fell through to "every day"
    because the parser swallowed its own error. Both are the kind of silence that
    matters here: this decides when a speaker turns itself on in someone's house.

    A ``{"schedule": {...}}`` wrapper is unwrapped rather than rejected: it is
    the shape GET returns, so round-tripping the response has to work.
    """
    if not isinstance(body, dict):
        raise ValueError("the schedule must be an object")
    if list(body.keys()) == ["schedule"] and isinstance(body["schedule"], dict):
        body = body["schedule"]

    unknown = [key for key in body if key not in _SCHEDULE_KEYS]
    if unknown:
        raise ValueError("unknown field(s): %s" % ", ".join(sorted(unknown)))

    result = {"enabled": bool(body.get("enabled", True))}  # type: Dict[str, Any]

    quiet = body.get("quiet_hours") or {}
    if not isinstance(quiet, dict):
        raise ValueError("quiet_hours must be an object with start and end")
    result["quiet_hours"] = {
        "start": _coerce_hhmm(quiet.get("start", "20:00"), "quiet_hours.start"),
        "end": _coerce_hhmm(quiet.get("end", "07:00"), "quiet_hours.end"),
    }

    windows_in = body.get("windows") or []
    if not isinstance(windows_in, list):
        raise ValueError("windows must be a list")
    windows = []  # type: List[Dict[str, Any]]
    for index, raw in enumerate(windows_in):
        if not isinstance(raw, dict):
            raise ValueError("windows[%d] must be an object" % index)
        unknown = [key for key in raw if key not in _WINDOW_KEYS]
        if unknown:
            raise ValueError("windows[%d]: unknown field(s): %s" % (index, ", ".join(sorted(unknown))))
        window = {
            "id": str(raw.get("id") or "window-%d" % index),
            "name": str(raw.get("name") or ""),
            "enabled": bool(raw.get("enabled", True)),
            "start": _coerce_hhmm(raw.get("start"), "windows[%d].start" % index),
            "end": _coerce_hhmm(raw.get("end"), "windows[%d].end" % index),
            "playlist_id": str(raw.get("playlist_id") or ""),
        }
        days = raw.get("days")
        if days is None:
            window["days"] = list(range(7))
        elif isinstance(days, (list, tuple)):
            try:
                window["days"] = sorted(set(_coerce_day(day) for day in days))
            except ValueError as exc:
                raise ValueError("windows[%d].%s" % (index, exc))
        else:
            raise ValueError("windows[%d].days must be a list" % index)
        if "volume" in raw:
            try:
                volume = float(raw["volume"])
            except (TypeError, ValueError):
                raise ValueError("windows[%d].volume must be a number between 0 and 1" % index)
            if not 0.0 <= volume <= 1.0:
                raise ValueError("windows[%d].volume must be between 0 and 1" % index)
            window["volume"] = volume
        windows.append(window)
    result["windows"] = _dedupe_windows(windows)
    return result


def _dedupe_windows(windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop repeats and make ids unique.

    Tapping a preset twice used to append the same window again, and the screen
    then showed "While I'm out 09:00 - 17:00" twice with no way to tell them
    apart or remove one. Two identical windows also mean nothing: the first one
    wins every time. Same id with different times is a different story -- that is
    a real second window, so it keeps its own id instead of being thrown away.
    """
    seen_shape = set()
    seen_ids = set()
    out = []  # type: List[Dict[str, Any]]
    for window in windows:
        shape = (
            window["name"], window["start"], window["end"],
            tuple(window["days"]), window["playlist_id"], window.get("volume"),
        )
        if shape in seen_shape:
            continue
        seen_shape.add(shape)
        window_id = window["id"]
        if window_id in seen_ids:
            suffix = 2
            while "%s-%d" % (window_id, suffix) in seen_ids:
                suffix += 1
            window_id = "%s-%d" % (window_id, suffix)
            window["id"] = window_id
        seen_ids.add(window_id)
        out.append(window)
    return out


def active_window(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first enabled window matching ``moment`` -- order is precedence (section 5)."""
    if not schedule_cfg.get("enabled", True):
        return None
    windows = schedule_cfg.get("windows") or []
    weekday = moment.weekday()
    now = moment.time()
    for window in windows:
        if not isinstance(window, dict) or not window.get("enabled", True):
            continue
        days = window.get("days")
        if days is not None:
            try:
                allowed = set(_coerce_day(day) for day in days)
            except (TypeError, ValueError) as exc:
                # A window nobody can read is a window that does not fire. The
                # old fallback here was "every day", which turns a typo in a
                # hand-edited config into a speaker playing at times nobody asked
                # for -- the one failure mode this app must not have.
                log.warning("ignoring window %r: %s", window.get("id", "?"), exc)
                continue
            if weekday not in allowed:
                continue
        start = _parse_hhmm(window.get("start"), _DEFAULT_TIME)
        end = _parse_hhmm(window.get("end"), _DEFAULT_TIME)
        if start <= end:
            hit = start <= now < end
        else:
            # Crosses midnight: the "today" half runs from start to 24:00,
            # the "tonight into tomorrow" half from 00:00 to end.
            hit = now >= start or now < end
        if hit:
            return window
    return None


def _would_play(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    window = active_window(moment, schedule_cfg)
    if window is None:
        return False, None
    if safety.is_quiet_hours(moment, schedule_cfg):
        return False, None
    return True, window


def _describe_moment(moment: dt.datetime, reference: dt.datetime) -> str:
    time_text = moment.strftime("%H:%M")
    if moment.date() == reference.date():
        return time_text
    if (moment.date() - reference.date()).days == 1:
        return "%s tomorrow" % time_text
    return "%s on %s" % (time_text, _WEEKDAY_NAMES[moment.weekday()])


def next_change(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What happens next, and when -- shown permanently in the UI (section 5).

    A scheduled app that looks idle is indistinguishable from a broken one, so
    this never just says "stopped"; it searches minute by minute, up to eight
    days out, for the next point where "would a session be playing" flips.
    """
    if not schedule_cfg.get("enabled", True):
        return {"event": "none", "at": None, "window_id": None, "message": "A agenda está desligada."}

    playing_now, current = _would_play(moment, schedule_cfg)
    cursor = moment
    step = dt.timedelta(minutes=1)
    for _ in range(60 * 24 * 8):
        cursor = cursor + step
        playing, window = _would_play(cursor, schedule_cfg)
        if playing != playing_now:
            when = _describe_moment(cursor, moment)
            if playing:
                return {
                    "event": "starts", "at": cursor.isoformat(),
                    "window_id": window.get("id") if window else None,
                    "message": "Toca às %s" % when,
                }
            return {
                "event": "stops", "at": cursor.isoformat(),
                "window_id": current.get("id") if current else None,
                "message": "Para às %s" % when,
            }
    return {
        "event": "none", "at": None, "window_id": None,
        "message": "Nada marcado para a próxima semana.",
    }


async def _maybe_call(callback: Optional[Callable[..., Any]], *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class SchedulerLoop(object):
    """Ticks once every ``interval`` seconds and calls back on a state change.

    Deliberately dumb: it asks :func:`_would_play` for the truth and only
    reacts to the answer changing, so "should we be playing" always has one
    source of truth that a test can call directly without an event loop.
    """

    def __init__(
        self,
        get_schedule: Callable[[], Dict[str, Any]],
        on_should_play: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_should_stop: Optional[Callable[[], Any]] = None,
        interval: float = 15.0,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ) -> None:
        self._get_schedule = get_schedule
        self._on_should_play = on_should_play
        self._on_should_stop = on_should_stop
        self._interval = float(interval)
        self._clock = clock or dt.datetime.now
        self._task = None  # type: Optional[asyncio.Future]
        self._active_window_id = None  # type: Optional[str]

    @property
    def active_window_id(self) -> Optional[str]:
        return self._active_window_id

    async def tick(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """One evaluation, exposed directly so tests do not need real sleeps."""
        moment = self._clock()
        schedule_cfg = self._get_schedule() or {}
        playing, window = _would_play(moment, schedule_cfg)
        window_id = window.get("id") if window else None
        if playing and window_id != self._active_window_id:
            self._active_window_id = window_id
            await _maybe_call(self._on_should_play, window)
        elif not playing and self._active_window_id is not None:
            self._active_window_id = None
            await _maybe_call(self._on_should_stop)
        return playing, window

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception:  # pragma: no cover - a bad tick must not kill the loop
                    log.warning("scheduler tick failed", exc_info=True)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


__all__ = [
    "PRESETS",
    "SchedulerLoop",
    "active_window",
    "next_change",
]
