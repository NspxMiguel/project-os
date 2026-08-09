"""Pure scoring and picking -- the whole point of the like / play-less buttons.

    "botao de curtir musicas, recomendar menos"

If feedback does not actually move selection probability, the buttons are
decoration. So this module has no side effects and no clock or randomness of
its own: :func:`score_track` takes ``now`` and the caller's recency list,
:func:`pick_next` takes an injected ``random.Random``, and every draw is
reproducible from a seed. That is what makes "liked tracks show up more"
a testable claim instead of a vibe.

Scoring is multiplicative, each factor independent and centred on 1.0, so
factors compose without needing to know about each other:

* ``like_boost`` raises the score per like.
* ``less_decay`` softens the score per "play this less", but a soft nudge
  must never reach zero -- the track stays reachable, just less likely.
* a dislike is treated as a stronger version of "less" (it carries the same
  per-vote decay, counted twice) *plus* its own decay on top, which is what
  makes a dislike consistently hit harder than an equal number of "less"
  votes without needing a second decay curve to reason about.
* ``novelty_bonus`` favours tracks that have not been played much.
* the recency window applies a heavy flat penalty to anything that appears
  in the caller's recently-played list, so shuffle does not immediately
  replay what just finished.

``blocked`` short-circuits everything: a blocked track scores exactly 0 and
never wins a draw, which is the difference between "recommend less" (a
nudge) and actually removing a track from rotation.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence

#: Fallbacks used when a caller's config is missing a knob.
_DEFAULTS = {
    "like_boost": 0.5,
    "dislike_decay": 0.5,
    "less_decay": 0.5,
    "recency_window": 8,
    "novelty_bonus": 0.2,
    "explore_rate": 0.0,
}

#: Recently-played tracks are not banned, just made very unlikely -- a small
#: library must still be able to replay something if nothing else is left.
_RECENCY_PENALTY = 0.05


def _cfg(config: Optional[Dict[str, Any]], key: str) -> float:
    config = config or {}
    try:
        return float(config.get(key, _DEFAULTS[key]))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def score_track(
    track: Dict[str, Any],
    config: Dict[str, Any],
    now: Any,
    recent_ids: Sequence[str],
) -> float:
    """A non-negative weight for one track. Zero means "never pick this"."""
    feedback = track.get("feedback") or {}
    if feedback.get("blocked"):
        return 0.0

    like_boost = _cfg(config, "like_boost")
    dislike_decay = min(1.0, max(0.0, _cfg(config, "dislike_decay")))
    less_decay = min(1.0, max(0.0, _cfg(config, "less_decay")))
    novelty_bonus = _cfg(config, "novelty_bonus")
    recency_window = int(_cfg(config, "recency_window"))

    likes = int(feedback.get("likes", 0) or 0)
    dislikes = int(feedback.get("dislikes", 0) or 0)
    less = int(feedback.get("less", 0) or 0)
    plays = int(feedback.get("plays", 0) or 0)

    score = 1.0
    score *= (1.0 + max(0.0, like_boost)) ** likes
    # A dislike is a double-weighted "less" plus its own decay on top, so it
    # is always worse than "less" alone for the same vote count.
    score *= (1.0 - less_decay) ** (less + dislikes * 2)
    score *= (1.0 - dislike_decay) ** dislikes
    # Fewer plays -> more novelty. The bonus shrinks as plays pile up rather
    # than disappearing outright, so a heavily-played favourite still gets a
    # sliver of it.
    score *= 1.0 + max(0.0, novelty_bonus) / (1.0 + max(0, plays))

    track_id = track.get("id")
    if recency_window > 0 and track_id in list(recent_ids or [])[-recency_window:]:
        score *= _RECENCY_PENALTY

    return max(0.0, score)


def pick_next(
    tracks: List[Dict[str, Any]],
    config: Dict[str, Any],
    now: Any,
    recent_ids: Sequence[str],
    rng: random.Random,
) -> Optional[Dict[str, Any]]:
    """One track out of ``tracks``, weighted by :func:`score_track`.

    ``rng`` is always the caller's -- never :mod:`random`'s module-level
    state -- so the same seed reproduces the same pick, which is what makes
    a scheduled shuffle debuggable ("what would it have played at 9am?").
    """
    if not tracks:
        return None

    explore_rate = min(1.0, max(0.0, _cfg(config, "explore_rate")))
    if explore_rate > 0.0 and rng.random() < explore_rate:
        # Pure exploration: ignore scores entirely for this draw.
        return rng.choice(tracks)

    weights = [score_track(t, config, now, recent_ids) for t in tracks]
    total = sum(weights)
    if total <= 0.0:
        # Everything scored zero (e.g. every candidate is blocked); fall
        # back to a uniform pick rather than returning nothing playable.
        return rng.choice(tracks)

    target = rng.uniform(0.0, total)
    upto = 0.0
    for track, weight in zip(tracks, weights):
        upto += weight
        if upto >= target:
            return track
    return tracks[-1]  # pragma: no cover - floating-point rounding only


def explain(
    track: Dict[str, Any],
    config: Dict[str, Any],
    now: Any,
    recent_ids: Sequence[str],
) -> Dict[str, Any]:
    """The factors behind a track's score, for a UI tooltip or a debug log."""
    feedback = track.get("feedback") or {}
    recency_window = int(_cfg(config, "recency_window"))
    recent_window = list(recent_ids or [])[-recency_window:] if recency_window > 0 else []
    return {
        "track_id": track.get("id"),
        "score": score_track(track, config, now, recent_ids),
        "likes": int(feedback.get("likes", 0) or 0),
        "dislikes": int(feedback.get("dislikes", 0) or 0),
        "less": int(feedback.get("less", 0) or 0),
        "plays": int(feedback.get("plays", 0) or 0),
        "blocked": bool(feedback.get("blocked")),
        "recently_played": track.get("id") in recent_window,
        "tags": list(track.get("tags") or []),
    }


__all__ = ["explain", "pick_next", "score_track"]
