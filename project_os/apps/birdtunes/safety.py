"""Server-side safety rules for BirdTunes.

These exist for one reason: there is a live animal on the other end of the
speaker. The UI can be wrong, a browser tab can be stale, an API client can
send anything -- none of that is allowed to translate into music playing at
2am or at a volume that startles the bird. So quiet hours, the volume
ceiling and the "can we even play right now" check are pure functions here,
called from the scheduler and from every API path that can start or change
playback, never trusted to have already been enforced upstream.

Nothing in this module touches the database, the clock or a device -- the
caller supplies ``now`` and the config, which is what makes the wrap-around-
midnight arithmetic and the boundary conditions easy to get right in tests
and to keep right during a refactor.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Tuple

#: Fallback used when a schedule config is missing the quiet-hours block.
_DEFAULT_START = "20:00"
_DEFAULT_END = "07:00"


def _parse_hhmm(value: Any, default: str) -> dt.time:
    """"HH:MM" -> a time. Anything unparsable falls back rather than crashing
    a scheduler tick over a typo in a config file."""
    text = str(value or default)
    try:
        hour, minute = text.split(":", 1)
        return dt.time(int(hour), int(minute))
    except (ValueError, TypeError):
        fallback_hour, fallback_minute = default.split(":")
        return dt.time(int(fallback_hour), int(fallback_minute))


def is_quiet_hours(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> bool:
    """Is ``moment`` inside the configured quiet-hours window?

    The window may cross midnight (``start > end``, e.g. 20:00-07:00), which
    is the normal case for a bird room: quiet starts in the evening and ends
    the next morning. ``start`` is inclusive, ``end`` is exclusive, so a
    window that ends at 07:00 lets music start exactly at 07:00. A
    zero-length window (``start == end``) is defined as never quiet -- the
    alternative reading, "quiet all day", would silently disable the app.
    """
    schedule_cfg = schedule_cfg or {}
    if not schedule_cfg.get("enabled", True):
        return False

    quiet = schedule_cfg.get("quiet_hours") or {}
    start = _parse_hhmm(quiet.get("start"), _DEFAULT_START)
    end = _parse_hhmm(quiet.get("end"), _DEFAULT_END)
    if start == end:
        return False

    now = moment.time()
    if start < end:
        # A same-day window, e.g. 13:00-14:00 for a midday nap.
        return start <= now < end
    # Wraps midnight, e.g. 20:00-07:00: quiet from start to midnight, and
    # from midnight to end.
    return now >= start or now < end


def clamp_volume(requested: Any, config: Dict[str, Any]) -> float:
    """``requested`` volume (0.0-1.0), never above the configured ceiling.

    The ceiling protects the bird from a client that sends 1.0 by mistake
    (or on purpose); it is not a suggestion the frontend is trusted to
    respect on its own.
    """
    config = config or {}
    output = config.get("output") or {}
    try:
        ceiling = float(output.get("max_volume", 0.6))
    except (TypeError, ValueError):
        ceiling = 0.6
    ceiling = max(0.0, min(1.0, ceiling))

    try:
        value = float(requested)
    except (TypeError, ValueError):
        value = 0.0
    if value != value:  # NaN
        value = 0.0

    if value < 0.0:
        return 0.0
    if value > ceiling:
        return ceiling
    return value


def check_can_play(
    moment: dt.datetime,
    schedule_cfg: Dict[str, Any],
    device_available: bool = True,
) -> Tuple[bool, str]:
    """Can playback start (or keep running) right now?

    Quiet hours beats everything, including a manual "play now" -- see
    ``docs/BIRDTUNES.md`` section 5: entering quiet hours stops playback in
    progress, it does not merely refuse to start it. An unreachable device
    is the next most common reason and gets its own actionable message so
    the scheduler's retry-with-backoff has something to show the user.
    """
    if is_quiet_hours(moment, schedule_cfg):
        return False, "Agora é hora do silêncio, então o BirdTunes não vai tocar."
    if not device_available:
        return False, "A saída de som não está acessível agora."
    return True, ""


__all__ = ["check_can_play", "clamp_volume", "is_quiet_hours"]
