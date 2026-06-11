"""EventBus — asyncio fan-out of events to N subscribers.

One Telethon handler publishes; every consumer subscribes independently.
Publishing never blocks: a full subscriber queue drops its oldest item so a
slow consumer can't stall the Telethon event loop. Generic over the event
type — the client runs separate buses for incoming/outgoing/deleted streams.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

DEFAULT_MAXSIZE = 100

T = TypeVar("T")


class EventBus(Generic[T]):
    def __init__(self, maxsize: int = DEFAULT_MAXSIZE):
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[T]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _register(self) -> asyncio.Queue[T]:
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def _unregister(self, queue: asyncio.Queue[T]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: T) -> None:
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()  # drop oldest
                    logger.warning(
                        "subscriber queue full (maxsize=%d), dropping oldest event",
                        self._maxsize,
                    )
                except asyncio.QueueEmpty:
                    # full() raced with a consumer that drained the queue —
                    # nothing was dropped after all (no-silent-failures: logged)
                    logger.debug("subscriber queue drained concurrently; nothing dropped")
            queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[T]:
        queue = self._register()
        try:
            while True:
                yield await queue.get()
        finally:
            self._unregister(queue)
