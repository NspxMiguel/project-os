"""A per-contact fixed-window rate limiter, in memory.

In memory, not in the database: the failure mode of "briefly more permissive
right after a restart" is nothing next to making every inbound message pay
for a database round-trip before the bot can even say "unknown command". The
window length is fixed at construction (how far back "recent" reaches); the
limit itself is passed to :meth:`allow` each call so it can follow a live
config change without rebuilding the limiter and losing its history.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional


class RateLimiter(object):
    def __init__(self, window_seconds: float = 60.0, clock: Optional[Callable[[], float]] = None) -> None:
        self.window_seconds = float(window_seconds)
        self._clock = clock or time.monotonic
        self._hits = {}  # type: Dict[str, List[float]]

    def allow(self, contact: str, max_per_window: int) -> bool:
        """True and records a hit when under the limit; False and records nothing."""
        limit = max(1, int(max_per_window))
        now = self._clock()
        cutoff = now - self.window_seconds
        hits = [t for t in self._hits.get(contact, []) if t > cutoff]
        if len(hits) >= limit:
            self._hits[contact] = hits
            return False
        hits.append(now)
        self._hits[contact] = hits
        return True

    def reset(self, contact: Optional[str] = None) -> None:
        if contact is None:
            self._hits.clear()
        else:
            self._hits.pop(contact, None)


__all__ = ["RateLimiter"]
