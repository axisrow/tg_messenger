import asyncio
from datetime import datetime, timezone

import pytest
from textual.widgets import Input, ListView, Static, Tabs

from tg_messenger.core.models import Dialog, IncomingEvent, Message
from tg_messenger.tui.app import (
    DialogItem,
    MessageBubble,
    MessengerTUI,
    ProfileItem,
    parse_media_command,
)


def test_parse_media_simple():
    assert parse_media_command("@a.jpg") == ("a.jpg", None)


def test_parse_media_quoted_path_with_caption():
    assert parse_media_command('@"с пробелом.png" подпись') == ("с пробелом.png", "подпись")


def test_parse_media_path_and_caption():
    assert parse_media_command("@/path/x.jpg caption here") == ("/path/x.jpg", "caption here")


def test_parse_media_non_at_is_none():
    assert parse_media_command("hello world") is None


def test_parse_media_empty_after_at_is_none():
    assert parse_media_command("@") is None
    assert parse_media_command("@   ") is None


class TuiStubClient:
    def __init__(self):
        self.sent = []
        self.read_acks = []
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
        dms = [Dialog(id=7, title="Ann [/x", username="ann", unread=0)]
        if dm_only:
            return dms
        # повторяет контракт core: dm_only=False — все диалоги с kind и marked id
        return dms + [
            Dialog(id=-100200, title="Devs", kind="group"),
            Dialog(id=9, title="HelperBot", kind="bot"),
        ]

    async def group_dialogs(self):
        return [d for d in await self.dialogs(dm_only=False) if d.kind != "dm"]

    async def history(self, peer, limit=50, offset_id=0):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False,
                        text="oops [/bad] [red",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def send_media(self, peer, file_path, *, caption=None, voice_note=False,
                         video_note=False, force_document=False):
        self.media_sent = (peer, str(file_path), caption)
        return Message(id=4, dialog_id=peer, sender_id=1, out=True, text=caption or "<media>",
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def mark_read(self, peer, max_id=None):
        self.read_acks.append((peer, max_id))

    async def listen_all(self):
        # idle forever; the worker just waits for events
        await asyncio.Event().wait()
        yield  # pragma: no cover


def test_real_tui_client_gets_session_encryption_key(monkeypatch, tmp_path):
    from tg_messenger.tui import app as tui_app

    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "shared-secret")
    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr("tg_messenger.core.client.StandaloneTelegramClient", FakeStandaloneTelegramClient)

    tui_app._make_real_client("default")

    assert captured["session_name"] == "default"
    assert captured["session_dir"] == str(tmp_path)
    assert captured["encryption_key"] == "shared-secret"


async def test_tui_mounts_and_lists_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        items = list(app.query(DialogItem))
        assert len(items) == 1
        assert items[0].dialog_id == 7


async def test_tui_dialog_item_shows_id():
    # цикл 63: DialogItem рендерит "id — title", id виден пользователю
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        # Static внутри DialogItem рендерит "7 — Ann [/x" литерально
        rendered = str(item.query_one(Static).render())
        assert rendered.startswith("7 — ")


class UnreadClient(TuiStubClient):
    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        return [Dialog(id=7, title="Ann", username="ann", unread=3)]


async def test_tui_dialog_item_shows_unread_count():
    # цикл 81: непрочитанные показываются как "(N)" в строке диалога
    app = MessengerTUI(client=UnreadClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        rendered = str(item.query_one(Static).render())
        assert "(3)" in rendered


async def test_tui_no_unread_marker_when_zero():
    app = MessengerTUI(client=TuiStubClient())  # Ann has unread=0
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        rendered = str(item.query_one(Static).render())
        assert "(" not in rendered.split("—", 1)[1]  # no "(N)" badge after the title


async def test_tui_selecting_dialog_marks_read():
    # цикл 81: открытие диалога помечает прочитанным (через worker, best-effort)
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        item = list(app.query(DialogItem))[0]
        await app.on_list_view_selected(ListView.Selected(lv, item, 0))
        await pilot.pause()
        await pilot.pause()
    assert stub.read_acks == [(7, 1)]


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

    stub.listen_all = broken_listen
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


@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"),
    reason="eager_task_factory is py3.12+; the regression it guards can't occur on 3.11",
)
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


# --- цикл 66: локальный поиск диалогов в TUI ---


async def test_tui_search_filters_dialogs():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert {item.dialog_id for item in app.query(DialogItem)} == {7, 8}
        search = app.query_one("#search", Input)
        search.value = "Bob"
        await app.on_input_changed(Input.Changed(search, "Bob"))
        await pilot.pause()
        # только Bob (id=8) остаётся видимым
        assert [item.dialog_id for item in app.query(DialogItem)] == [8]


async def test_tui_search_clear_restores_full_list():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "Bob"
        await app.on_input_changed(Input.Changed(search, "Bob"))
        await pilot.pause()
        search.value = ""
        await app.on_input_changed(Input.Changed(search, ""))
        await pilot.pause()
        assert {item.dialog_id for item in app.query(DialogItem)} == {7, 8}


async def test_tui_search_does_not_hit_network():
    stub = TwoDmClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls_before = stub.dialogs_calls
        search = app.query_one("#search", Input)
        search.value = "Bob"
        await app.on_input_changed(Input.Changed(search, "Bob"))
        await pilot.pause()
        # фильтрация локальная — поверх уже загруженного списка, без запроса
        assert stub.dialogs_calls == calls_before


# --- Цикл 36: вкладки DM / Группы ---


def _listed_ids(app):
    return [item.dialog_id for item in app.query(DialogItem)]


async def test_tui_has_tabs_dm_active_by_default():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        assert tabs.active == "dm"
        assert _listed_ids(app) == [7]


async def test_tui_groups_tab_lists_non_dm_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "groups"
        await pilot.pause()
        assert _listed_ids(app) == [-100200, 9]  # без DM


async def test_tui_tab_switch_back_reloads_dm():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        tabs.active = "groups"
        await pilot.pause()
        tabs.active = "dm"
        await pilot.pause()
        assert _listed_ids(app) == [7]  # список перезагружен, не накоплен


async def test_tui_tab_activation_before_startup_is_safe():
    # переключение вкладки, пока connect ещё висит, не должно дёргать сеть
    stub = TuiStubClient()
    connect_entered = asyncio.Event()
    stub.connect = hangs_forever(connect_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await asyncio.wait_for(connect_entered.wait(), 5)
        await pilot.pause()
        app.query_one(Tabs).active = "groups"
        await pilot.pause()
        assert stub.dialogs_calls == 0  # клиент ещё не готов — запроса не было
        assert app.return_code is None  # и приложение живо
        await pilot.press("ctrl+c")
    assert app.return_code == 0


# --- Цикл 37: live-входящие из групп ---


class GroupEventClient(TuiStubClient):
    """listen_all, который по сигналу отдаёт два события: DM и групповое."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(dialog_id=7, message=Message(
            id=20, dialog_id=7, sender_id=7, out=False, text="из ЛС", date=date))
        yield IncomingEvent(dialog_id=-100200, message=Message(
            id=21, dialog_id=-100200, sender_id=9, out=False, text="из группы", date=date))
        await asyncio.Event().wait()  # idle forever


async def test_tui_group_incoming_appends_bubble_for_open_group_only():
    stub = GroupEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100200  # открыта группа
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        # ЛС-событие не дорисовано (чужой диалог), групповое — да
        assert [str(b.render()) for b in bubbles] == ["из группы"]


async def test_tui_group_incoming_does_not_trigger_suggester():
    class RecordingSuggester:
        def __init__(self):
            self.calls = []

        async def suggest(self, dialog_id):
            self.calls.append(dialog_id)
            return "draft"

    stub = GroupEventClient()
    suggester = RecordingSuggester()
    app = MessengerTUI(client=stub, suggester=suggester)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100200  # open group, but suggestion must stay DM-only
        stub.fire.set()
        await pilot.pause()
    assert suggester.calls == []


async def test_tui_disconnects_on_exit():
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert stub.connected is True
    assert stub.connected is False


async def test_tui_closes_suggester_on_exit():
    class ClosableSuggester:
        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    suggester = ClosableSuggester()
    app = MessengerTUI(client=TuiStubClient(), suggester=suggester)
    async with app.run_test() as pilot:
        await pilot.pause()
    assert suggester.closed == 1


async def test_tui_switching_dialogs_clears_pending_suggestion():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._pending_suggestion = "draft for Ann"
        app.query_one("#suggestion", Static).update("Suggest: draft for Ann")
        lv = app.query_one("#dialogs", ListView)
        lv.index = 1
        lv.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert app._pending_suggestion is None
        assert str(app.query_one("#suggestion", Static).render()) == ""


# --- UX: Enter / стрелка-вниз с вкладок → фокус на список диалогов ---


async def test_down_arrow_on_tabs_moves_focus_to_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        tabs.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_enter_on_tabs_moves_focus_to_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        tabs.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_down_focuses_first_dialog_so_it_is_navigable():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        assert lv.index == 0  # списком сразу можно листать


# --- UX: стрелка-вверх на первом диалоге → обратно на вкладки DM/Группы ---


async def test_up_on_first_dialog_returns_focus_to_tabs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0  # первый элемент
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one(Tabs)


async def test_up_on_non_first_dialog_scrolls_list_not_tabs():
    """Со второго диалога ↑ листает список вверх, фокус остаётся на списке."""
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 1  # второй элемент
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is lv  # фокус не ушёл на вкладки
        assert lv.index == 0  # поднялись на первый


class TwoDmClient(TuiStubClient):
    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        dms = [
            Dialog(id=7, title="Ann", username="ann", unread=0),
            Dialog(id=8, title="Bob", username="bob", unread=0),
        ]
        if dm_only:
            return dms
        return dms + [Dialog(id=-100200, title="Devs", kind="group")]


# --- цикл 60: TUI выбор профиля (мультилогин) ---

async def test_tui_profile_screen_picks_and_builds_client():
    captured = {}

    def factory(session_name):
        captured["session_name"] = session_name
        return TuiStubClient()

    app = MessengerTUI(profiles=["alice", "bob"], client_factory=factory)
    async with app.run_test() as pilot:
        # wait for the pushed profile screen to mount (it's a modal — query the screen)
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(ProfileItem):
                break
        assert len(list(app.screen.query(ProfileItem))) == 2
        # select the second profile (alice, bob -> bob)
        lv = app.screen.query_one("#profiles", ListView)
        lv.index = 1
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if captured.get("session_name"):
                break
    assert captured.get("session_name") == "bob"


async def test_tui_single_profile_skips_screen():
    captured = {}

    def factory(session_name):
        captured["session_name"] = session_name
        return TuiStubClient()

    app = MessengerTUI(profiles=["solo"], client_factory=factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert list(app.query(ProfileItem)) == []  # no selection screen
        assert list(app.query(DialogItem))  # went straight to dialogs
    assert captured.get("session_name") == "solo"


async def test_tui_at_command_sends_media(tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, f"@{f} cap"))
        await pilot.pause()
        await pilot.pause()
        assert getattr(stub, "media_sent", None) == (7, str(f), "cap")
        bubbles = list(app.query(MessageBubble))
        assert any("cap" in str(b.render()) for b in bubbles)


async def test_tui_at_command_missing_file_notifies(tmp_path):
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        missing = str(tmp_path / "nope.jpg")
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, f"@{missing}"))
        await pilot.pause()
        assert getattr(stub, "media_sent", None) is None
        assert app.return_code is None  # still alive
