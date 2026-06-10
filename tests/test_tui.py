import asyncio
from datetime import datetime, timezone

from textual.widgets import Input, ListView

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


async def test_tui_startup_failure_exits_with_code_and_log(caplog):
    stub = TuiStubClient()

    async def boom(dm_only=True):
        raise RuntimeError("startup blew up")

    stub.dialogs = boom
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
    assert app.return_code == 1
    assert stub.connected is False
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_tui_send_failure_notifies_instead_of_crashing(caplog):
    stub = TuiStubClient()

    async def boom(peer, text):
        raise RuntimeError("send blew up")

    stub.send_text = boom
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = 7
            composer = app.query_one("#composer", Input)
            await app.on_input_submitted(Input.Submitted(composer, "hi"))
            await pilot.pause()
            assert app.return_code is None  # still alive
            assert list(app.query(MessageBubble)) == []  # nothing mounted
            assert composer.value == "hi"  # draft is given back, not lost
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_tui_listener_failure_logged_app_stays_alive(caplog):
    stub = TuiStubClient()

    async def broken_listen():
        raise RuntimeError("listener blew up")
        yield  # pragma: no cover

    stub.listen = broken_listen
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.return_code is None  # worker died, app did not
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


class EagerSensitiveClient(TuiStubClient):
    """Mimics Telethon's MTProtoSender startup race.

    ``connect()`` spawns a pump task via ``loop.create_task`` and sets the
    running flag only AFTER — exactly like telethon sets ``_user_connected``
    after starting ``_send_loop``. Under ``asyncio.eager_task_factory`` (which
    Textual's real ``App.run()`` installs on py3.12+) the pump body runs at
    creation time, sees the flag still False and dies — every later request
    then waits forever, which is the "TUI connects but never loads" bug.
    """

    def __init__(self):
        super().__init__()
        self._running = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def _pump(self):
        while self._running:
            fut = await self._queue.get()
            fut.set_result(None)

    async def connect(self):
        self._pump_task = asyncio.get_running_loop().create_task(self._pump())
        self._running = True  # after create_task, like mtprotosender.py:134
        await super().connect()

    async def dialogs(self, dm_only=True):
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put(fut)
        await fut  # never resolves if the pump died at creation
        return await super().dialogs(dm_only=dm_only)


def hangs_forever(entered: asyncio.Event):
    """Stub coroutine factory: signals entry, then never returns."""

    async def hung(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()  # never resolves

    return hung


async def test_tui_loads_dialogs_under_eager_task_factory():
    # the real App.run() installs eager_task_factory on the loop; run_test()
    # does not, which is why this regression was invisible to every other test
    loop = asyncio.get_running_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        app = MessengerTUI(client=EagerSensitiveClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(list(app.query(DialogItem))) == 1
    finally:
        loop.set_task_factory(None)


async def test_tui_history_load_does_not_block_ui():
    stub = TuiStubClient()
    history_entered = asyncio.Event()
    stub.history = hangs_forever(history_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        item = list(app.query(DialogItem))[0]
        # the handler must return immediately, not await the network
        await asyncio.wait_for(app.on_list_view_selected(ListView.Selected(lv, item, 0)), 5)
        await asyncio.wait_for(history_entered.wait(), 5)
        await pilot.press("ctrl+c")  # quit works while history hangs
    assert app.return_code == 0


async def test_tui_history_failure_notifies_instead_of_crashing(caplog):
    stub = TuiStubClient()

    async def boom(peer, limit=50, offset_id=0):
        raise RuntimeError("history blew up")

    stub.history = boom
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._show_history(7)
            await pilot.pause()
            assert app.return_code is None  # still alive
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_tui_send_does_not_block_ui():
    stub = TuiStubClient()
    send_entered = asyncio.Event()
    stub.send_text = hangs_forever(send_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        composer = app.query_one("#composer", Input)
        await asyncio.wait_for(app.on_input_submitted(Input.Submitted(composer, "hi")), 5)
        await asyncio.wait_for(send_entered.wait(), 5)
        assert composer.value == ""  # cleared optimistically while sending
        await pilot.press("ctrl+c")  # quit works while send hangs
    assert app.return_code == 0


async def test_tui_stays_responsive_and_quits_while_startup_hangs():
    # a hung network must not freeze the UI: the screen renders, keys are
    # processed, and ctrl+c / ctrl+q quit even before startup completes
    stub = TuiStubClient()
    connect_entered = asyncio.Event()
    stub.connect = hangs_forever(connect_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await asyncio.wait_for(connect_entered.wait(), 5)
        await pilot.pause()
        assert stub.dialogs_calls == 0  # still stuck in connect…
        await pilot.press("ctrl+c")  # …yet quitting must work
    assert app.return_code == 0  # clean quit, not a crash


async def test_tui_shows_loading_until_dialogs_arrive():
    stub = TuiStubClient()
    gate = asyncio.Event()
    real_dialogs = stub.dialogs

    async def gated_dialogs(dm_only=True):
        await gate.wait()
        return await real_dialogs(dm_only=dm_only)

    stub.dialogs = gated_dialogs
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#dialogs", ListView).loading is True
        gate.set()
        await pilot.pause()
        assert app.query_one("#dialogs", ListView).loading is False
        assert len(list(app.query(DialogItem))) == 1


async def test_tui_disconnects_on_exit():
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert stub.connected is True
    assert stub.connected is False
