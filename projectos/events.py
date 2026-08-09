"""In-process event bus.

One publisher, many subscribers, bounded queues. A subscriber that cannot keep
up loses its *oldest* events -- it never slows the publisher down, because the
publisher is usually the discovery scanner or a player callback that must not
stall.

Usage::

    async with bus.subscribe(["system.stats", "app.*"]) as events:
        async for event in events:
            ...
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 256

_CLOSED = object()


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class Event(object):
    """``{"topic": str, "ts": iso8601, "data": dict}``.

    Attribute access and mapping access both work, so consumers can do
    ``event.topic`` or ``event["topic"]``, and ``dict(event)`` produces the
    wire shape directly.
    """

    __slots__ = ("topic", "ts", "data")

    def __init__(
        self,
        topic: str,
        data: Optional[Dict[str, Any]] = None,
        ts: Optional[str] = None,
    ) -> None:
        self.topic = topic
        self.ts = ts or _utcnow_iso()
        self.data = data if data is not None else {}

    def to_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic, "ts": self.ts, "data": self.data}

    as_dict = to_dict

    def keys(self) -> List[str]:
        return ["topic", "ts", "data"]

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default) if key in ("topic", "ts", "data") else default

    def __getitem__(self, key: str) -> Any:
        if key in ("topic", "ts", "data"):
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __repr__(self) -> str:
        return "Event(topic=%r, ts=%r, data=%r)" % (self.topic, self.ts, self.data)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Event):
            return self.to_dict() == other.to_dict()
        if isinstance(other, dict):
            return self.to_dict() == other
        return NotImplemented

    def __hash__(self) -> int:  # pragma: no cover - events are rarely hashed
        return hash((self.topic, self.ts))


def _topic_matches(topic: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern in ("*", "#"):
            return True
        if pattern.endswith("*"):
            if topic.startswith(pattern[:-1]):
                return True
        elif topic == pattern:
            return True
    return False


class Subscription(object):
    """An async iterator of events, registered with the bus while it lives.

    It is also an async context manager; ``__aexit__`` always unregisters, even
    when the consumer raises or is cancelled.
    """

    def __init__(
        self,
        bus: "EventBus",
        topics: Optional[Iterable[str]] = None,
        maxsize: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._bus = bus
        self._topics = tuple(topics) if topics else ()
        self._queue = asyncio.Queue(maxsize=maxsize)  # type: asyncio.Queue
        self.dropped = 0
        self.closed = False

    # -- registration ----------------------------------------------------
    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._bus._unregister(self)
        try:
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            # Make room; the consumer is going away anyway.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(_CLOSED)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass

    # -- iteration -------------------------------------------------------
    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Event:
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    async def get(self, timeout: Optional[float] = None) -> Optional[Event]:
        """Next event, or ``None`` on timeout / closed subscription."""
        try:
            if timeout is None:
                item = await self._queue.get()
            else:
                item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if item is _CLOSED:
            return None
        return item

    # -- delivery --------------------------------------------------------
    def matches(self, topic: str) -> bool:
        if not self._topics:
            return True
        return _topic_matches(topic, self._topics)

    def _deliver(self, event: Event) -> None:
        if self.closed or not self.matches(event.topic):
            return
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - racy, retry
                    return

    @property
    def qsize(self) -> int:
        return self._queue.qsize()


class EventBus(object):
    """Fan-out of :class:`Event` objects to every live subscription."""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self.maxsize = int(maxsize)
        self._subs = []  # type: List[Subscription]
        self._loop = None  # type: Optional[asyncio.AbstractEventLoop]
        self.published = 0

    # -- loop binding ----------------------------------------------------
    def set_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Bind the bus to the loop that owns the subscriber queues.

        Required before :meth:`publish_nowait` can be called safely from worker
        threads. ``set_loop()`` with no argument binds the running loop.
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        self._loop = loop

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    # -- subscription ----------------------------------------------------
    def subscribe(
        self,
        topics: Optional[Iterable[str]] = None,
        maxsize: Optional[int] = None,
    ) -> Subscription:
        """Register a subscription and return it.

        ``topics`` accepts exact topics and prefix patterns (``"app.*"``);
        ``None`` means every topic.
        """
        sub = Subscription(self, topics=topics, maxsize=maxsize or self.maxsize)
        self._subs.append(sub)
        if self._loop is None:
            self.set_loop()
        return sub

    def _unregister(self, sub: Subscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # -- publishing ------------------------------------------------------
    def _dispatch(self, event: Event) -> None:
        self.published += 1
        for sub in list(self._subs):
            try:
                sub._deliver(event)
            except Exception:  # pragma: no cover - a broken subscriber is its own problem
                log.debug("event delivery failed for topic %s", event.topic, exc_info=True)

    async def publish(self, topic: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self._loop is None:
            self.set_loop()
        self._dispatch(Event(topic, data))

    def publish_nowait(self, topic: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Publish from sync code, including non-loop threads."""
        event = Event(topic, data)
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            try:
                loop.call_soon_threadsafe(self._dispatch, event)
            except RuntimeError:
                # Loop already closed (shutdown race) -- dropping is correct.
                pass
            return
        if loop is None and running is not None:
            self._loop = running
        self._dispatch(event)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        """Close every subscription; iterators stop cleanly."""
        for sub in list(self._subs):
            sub.close()
        self._subs = []


__all__ = ["Event", "EventBus", "Subscription", "DEFAULT_QUEUE_SIZE"]
