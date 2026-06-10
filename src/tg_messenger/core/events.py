"""EventBus — asyncio fan-out of incoming messages to N subscribers.

One Telethon NewMessage handler publishes; every UI subscribes independently.
Publishing never blocks: a full subscriber queue drops its oldest item so a
slow consumer can't stall the Telethon event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from tg_messenger.core.models import IncomingEvent

logger = logging.getLogger(__name__)

DEFAULT_MAXSIZE = 100


class EventBus:
    def __init__(self, maxsize: int = DEFAULT_MAXSIZE):
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[IncomingEvent]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _register(self) -> asyncio.Queue[IncomingEvent]:
        queue: asyncio.Queue[IncomingEvent] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def _unregister(self, queue: asyncio.Queue[IncomingEvent]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: IncomingEvent) -> None:
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()  # drop oldest
                    logger.warning(
                        "subscriber queue full (maxsize=%d), dropping oldest event",
                        self._maxsize,
                    )
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[IncomingEvent]:
        queue = self._register()
        try:
            while True:
                yield await queue.get()
        finally:
            self._unregister(queue)
