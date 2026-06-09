from datetime import datetime, timezone

from tg_messenger.core.models import Dialog, Message
from tg_messenger.tui.app import DialogItem, MessengerTUI


class TuiStubClient:
    def __init__(self):
        self.sent = []
        self._never = None

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def dialogs(self, dm_only=True):
        return [Dialog(id=7, title="Ann", username="ann", unread=0)]

    async def history(self, peer, limit=50, offset_id=0):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def send_text(self, peer, text):
        self.sent.append((peer, text))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def listen(self):
        import asyncio

        # idle forever; the worker just waits for events
        await asyncio.Event().wait()
        yield  # pragma: no cover


async def test_tui_mounts_and_lists_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        items = list(app.query(DialogItem))
        assert len(items) == 1
        assert items[0].dialog_id == 7
