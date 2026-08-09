"""BirdTunes -- a scheduled music player for pet birds.

The app keeps a library of audio files (and internet-radio URLs), learns from
like / dislike / "play this less", and casts to a real speaker on the LAN
through the backends in :mod:`projectos.apps.birdtunes.players`.

Layout::

    app.py          setup(ctx) -> AppInstance, the HTTP API, the signed media URL
    library.py      the track store (scan, search, feedback, history)
    metadata.py     tag reading and the keyword table that derives default tags
    recommender.py  pure scoring / picking, deterministic with an injected rng
    safety.py       quiet hours, volume ceiling, session limits -- server-side
    scheduler.py    the session loop: starts, advances, fades, stops

Nothing here imports an optional dependency at module level: the app must load
on a Pi that has neither pyatv nor pychromecast installed, and say so clearly
instead of failing to start.
"""

from __future__ import annotations

APP_ID = "birdtunes"

__all__ = ["APP_ID"]
