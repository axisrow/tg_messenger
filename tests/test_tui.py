from datetime import datetime, timezone

from tg_messenger.core.models import Dialog, Message
from tg_messenger.tui.app import DialogItem, MessageBubble, MessengerTUI


class TuiStubClient:
    def __init__(self):
        self.sent = []
        self.connected = False
        self.authorized = True
        self.dialogs_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_authorized(self):
        return self.authorized

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        # title contains markup-hostile brackets on purpose
        return [Dialog(id=7, title="Ann [/x", username="ann", unread=0)]

    async def history(self, peer, limit=50, offset_id=0):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False,
                        text="oops [/bad] [red",
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


async def test_tui_survives_markup_hostile_text():
    # dialog titles and message text with [brackets] must render literally,
    # not be parsed as Textual markup (which raises MarkupError)
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) == 1


async def test_tui_exits_with_hint_when_not_logged_in():
    stub = TuiStubClient()
    stub.authorized = False
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
    assert app.return_code == 1
    assert stub.dialogs_calls == 0  # dialogs were never requested
    assert stub.connected is False


async def test_tui_disconnects_on_exit():
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert stub.connected is True
    assert stub.connected is False
