import asyncio
from datetime import datetime, timezone

from tg_messenger.core.events import EventBus
from tg_messenger.core.models import IncomingEvent, Message


def _event(text="hi"):
    msg = Message(
        id=1, dialog_id=7, sender_id=42, out=False, text=text,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    return IncomingEvent(dialog_id=7, message=msg)


async def test_publish_reaches_all_subscribers():
    bus = EventBus()

    async def collect(n):
        out = []
        async for ev in bus.subscribe():
            out.append(ev)
            if len(out) == n:
                return out

    t1 = asyncio.create_task(collect(1))
    t2 = asyncio.create_task(collect(1))
    await asyncio.sleep(0)  # let subscribers register

    bus.publish(_event("a"))
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1)
    assert r1[0].message.text == "a"
    assert r2[0].message.text == "a"


async def test_unsubscribe_on_cancel_cleans_up():
    bus = EventBus()

    async def collect():
        async for _ in bus.subscribe():
            pass

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert bus.subscriber_count == 0


async def test_overflow_drops_oldest_without_blocking():
    bus = EventBus(maxsize=2)
    queue = bus._register()  # internal handle for the test
    try:
        # publish 3 into a size-2 queue: oldest dropped, never blocks
        bus.publish(_event("1"))
        bus.publish(_event("2"))
        bus.publish(_event("3"))
        assert queue.qsize() == 2
        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)
        assert first.message.text == "2"
        assert second.message.text == "3"
    finally:
        bus._unregister(queue)
