import asyncio
import logging

import pytest
from telethon import events
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser

from tests.conftest import (
    FakeAdminRights,
    FakeBannedRights,
    FakeChannel,
    FakeChat,
    FakeChatActionEvent,
    FakeDialog,
    FakeDocument,
    FakeMessage,
    FakeMessageReadEvent,
    FakeUser,
)
from tg_messenger.core import client as client_module
from tg_messenger.core.client import (
    READ_ONLY_MESSAGE,
    MessageDeleteValidationError,
    SendForbiddenError,
    StandaloneTelegramClient,
    _dialog_kind,
    _entity_can_send,
    _forbidden_message,
)
from tg_messenger.core.models import (
    ChatActionEvent,
    Dialog,
    IncomingEvent,
    Message,
    MessageReadEvent,
    MessagesDeletedEvent,
    OutgoingEvent,
    ReactionEvent,
    User,
)

VALID_SESSION = StringSession().save()  # valid, empty session string


def _build(fake_client, **kw):
    return StandaloneTelegramClient(
        api_id=1,
        api_hash="h",
        client_factory=lambda session, api_id, api_hash: fake_client,
        **kw,
    )


def _seed_dm(fake_client):
    ann = FakeUser(id=7, first_name="Ann", username="ann")
    bob = FakeUser(id=8, first_name="Bob")
    chan = FakeChannel(id=100, title="News", username="news")
    fake_client.dialogs = [
        FakeDialog(ann, name="Ann", unread_count=2,
                   message=FakeMessage(id=5, sender_id=7, text="hey")),
        FakeDialog(bob, name="Bob"),
        FakeDialog(chan, name="News"),  # not a DM
    ]
    # Telethon iter_messages yields newest-first
    fake_client.messages[7] = [
        FakeMessage(id=2, sender_id=1, text="yo", out=True),
        FakeMessage(id=1, sender_id=7, text="hi", out=False),
    ]


async def test_connect_disconnect(fake_client):
    client = _build(fake_client)
    await client.connect()
    assert fake_client.connected is True
    await client.disconnect()
    assert fake_client.connected is False


async def test_dialogs_dm_only(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=True)
    assert all(isinstance(d, Dialog) for d in dialogs)
    ids = {d.id for d in dialogs}
    assert ids == {7, 8}  # channel 100 excluded
    ann = next(d for d in dialogs if d.id == 7)
    assert ann.title == "Ann"
    assert ann.unread == 2
    assert ann.last_text == "hey"
    assert ann.last_message_at is not None


# --- Цикл 32: dialogs(dm_only=False) — все виды, marked id ---


def _seed_all_kinds(fake_client):
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann", username="ann", contact=True), name="Ann"),
        FakeDialog(FakeUser(id=9, first_name="Helper", bot=True), name="HelperBot"),
        FakeDialog(FakeChat(id=50, title="Devs"), name="Devs"),
        FakeDialog(FakeChannel(id=123, title="News", broadcast=True), name="News"),
        FakeDialog(FakeChannel(id=200, title="SG", broadcast=False), name="SG"),
    ]


async def test_dialogs_all_returns_every_kind_with_marked_ids(fake_client):
    _seed_all_kinds(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=False)
    # id — marked (отрицательный для групп/каналов): совпадает с event.chat_id
    assert {d.id: d.kind for d in dialogs} == {
        7: "dm", 9: "bot", -50: "group", -100123: "channel", -100200: "group",
    }
    assert {d.id: d.title for d in dialogs}[-50] == "Devs"
    assert next(d for d in dialogs if d.id == 7).is_contact is True
    assert next(d for d in dialogs if d.id == 9).is_contact is None
    assert all(d.archived is False for d in dialogs)


async def test_dialogs_maps_supported_dm_telegram_lang_code(fake_client):
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann", lang_code="EN"), name="Ann"),
    ]
    client = _build(fake_client)
    await client.connect()

    (dialog,) = await client.dialogs(dm_only=False)

    assert dialog.telegram_lang_code == "en"


async def test_dialogs_ignore_unsupported_and_non_dm_telegram_lang_code(fake_client):
    channel = FakeChannel(id=123, title="News", broadcast=True)
    channel.lang_code = "en"
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann", lang_code="fr"), name="Ann"),
        FakeDialog(channel, name="News"),
    ]
    client = _build(fake_client)
    await client.connect()

    dialogs = await client.dialogs(dm_only=False)

    assert {d.id: d.telegram_lang_code for d in dialogs} == {7: None, -100123: None}


async def test_dialogs_dm_only_excludes_bots_and_groups(fake_client):
    _seed_all_kinds(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=True)
    assert [d.id for d in dialogs] == [7]
    assert dialogs[0].kind == "dm"


async def test_archived_dialogs_are_separate_from_normal_dialogs(fake_client):
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann", contact=True), name="Ann"),
        FakeDialog(FakeUser(id=8, first_name="Old", contact=False), name="Old", archived=True),
        FakeDialog(FakeChannel(id=123, title="Archive News", broadcast=True), name="Archive News", archived=True),
    ]
    client = _build(fake_client)
    await client.connect()

    normal = await client.dialogs(dm_only=False)
    archived = await client.archived_dialogs()

    assert [d.id for d in normal] == [7]
    assert [d.id for d in archived] == [8, -100123]
    assert all(d.archived is True for d in archived)
    assert archived[0].is_contact is False


# --- Цикл 31: классификатор вида диалога ---


@pytest.mark.parametrize(
    ("entity", "kind"),
    [
        (FakeUser(id=7, first_name="Ann"), "dm"),
        (FakeUser(id=9, first_name="Helper", bot=True), "bot"),
        (FakeChat(id=50, title="Devs"), "group"),  # малая группа: title, без broadcast
        (FakeChannel(id=200, title="SG", broadcast=False), "group"),  # супергруппа
        (FakeChannel(id=123, title="News", broadcast=True), "channel"),  # бродкаст
    ],
)
def test_dialog_kind_classifier(entity, kind):
    assert _dialog_kind(entity) == kind


def test_unknown_entity_is_not_dm():
    # fail-safe прежней _is_dm_entity-семантики: ни title, ни имён → НЕ DM
    assert _dialog_kind(object()) != "dm"


@pytest.mark.parametrize(
    ("entity", "can_send"),
    [
        # DM / бот — нет title → всегда writable
        (FakeUser(id=7, first_name="Ann"), True),
        (FakeUser(id=9, first_name="Helper", bot=True), True),
        # read-only broadcast: обычный подписчик (наш кейс из скриншота)
        (FakeChannel(id=1, title="News", broadcast=True), False),
        # broadcast-админ с правом постить
        (FakeChannel(id=2, title="News", broadcast=True,
                     admin_rights=FakeAdminRights(post_messages=True)), True),
        # broadcast-админ БЕЗ права постить (только модерация) → read-only
        (FakeChannel(id=3, title="News", broadcast=True,
                     admin_rights=FakeAdminRights(post_messages=False)), False),
        # creator канала
        (FakeChannel(id=4, title="News", broadcast=True, creator=True), True),
        # супергруппа: чат-wide ограничение отправки → read-only
        (FakeChannel(id=5, title="SG", broadcast=False,
                     default_banned_rights=FakeBannedRights(send_messages=True)), False),
        # обычная writable супергруппа
        (FakeChannel(id=6, title="SG", broadcast=False), True),
        # обычная writable малая группа
        (FakeChat(id=7, title="Devs"), True),
        # админ супергруппы пишет вопреки default_banned_rights
        (FakeChannel(id=8, title="SG", broadcast=False,
                     admin_rights=FakeAdminRights(),
                     default_banned_rights=FakeBannedRights(send_messages=True)), True),
        # персональное ограничение (нас замутили) → read-only
        (FakeChannel(id=9, title="SG", broadcast=False,
                     banned_rights=FakeBannedRights(send_messages=True)), False),
        # вышли из чата → read-only
        (FakeChannel(id=10, title="SG", broadcast=False, left=True), False),
        # неизвестная форма entity — fail-safe в writable
        (object(), True),
    ],
)
def test_entity_can_send(entity, can_send):
    assert _entity_can_send(entity) is can_send


async def test_dialogs_populates_can_send(fake_client):
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann"), name="Ann"),
        FakeDialog(FakeChannel(id=123, title="News", broadcast=True), name="News"),
        FakeDialog(FakeChat(id=50, title="Devs"), name="Devs"),
        FakeDialog(FakeChannel(id=200, title="SG", broadcast=False,
                               default_banned_rights=FakeBannedRights(send_messages=True)),
                   name="Locked"),
    ]
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=False)
    assert {d.id: d.can_send for d in dialogs} == {
        7: True,          # DM
        -100123: False,   # read-only broadcast
        -50: True,        # writable group
        -100200: False,   # chat-wide restricted group
    }


async def test_can_post_to_resolves_from_cached_dialogs(fake_client):
    # #90: the single capability resolver — reads the cached dialog list, no recompute.
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann"), name="Ann"),
        FakeDialog(FakeChannel(id=123, title="News", broadcast=True), name="News"),
    ]
    client = _build(fake_client)
    await client.connect()
    assert await client.can_post_to(7) is True            # writable DM
    assert await client.can_post_to(-100123) is False     # read-only broadcast
    assert await client.can_post_to(999) is True          # unknown → fail-safe writable


async def test_can_post_to_uses_the_cache_no_second_fetch(fake_client):
    # warm-cache call must not hit the network a second time (rides the #8 TTL cache)
    fake_client.dialogs = [FakeDialog(FakeUser(id=7, first_name="Ann"), name="Ann")]
    client = _build(fake_client)
    await client.connect()
    await client.dialogs(dm_only=False)  # warm the cache
    before = fake_client.iter_dialogs_calls
    assert await client.can_post_to(7) is True
    assert fake_client.iter_dialogs_calls == before  # no extra fetch


async def test_can_post_to_permissive_on_lookup_failure(fake_client):
    # a dialogs() failure must not lock the chat — return True; SendForbiddenError is the net
    client = _build(fake_client)
    await client.connect()

    async def boom(dm_only=False):
        raise RuntimeError("dialogs unavailable")

    client.dialogs = boom  # type: ignore[method-assign]
    assert await client.can_post_to(-100123) is True


_RIGHTS_ERROR_NAMES = [
    "ChatAdminRequiredError",
    "ChatWriteForbiddenError",
    "ChatSendMediaForbiddenError",
    "UserBannedInChannelError",
    "ChatGuestSendForbiddenError",
    "ChatRestrictedError",
    "ChatSendGifsForbiddenError",
    "ChatSendStickersForbiddenError",
    "ChatSendPollForbiddenError",
    "VoiceMessagesForbiddenError",
    "ChatForbiddenError",  # "You cannot write in this chat" — read-only after a stale cache
]


def _rights_error(name):
    from telethon import errors

    return getattr(errors, name)(request=None)


@pytest.mark.parametrize("err_name", _RIGHTS_ERROR_NAMES)
async def test_send_text_classifies_rights_errors(fake_client, err_name):
    client = _build(fake_client)
    await client.connect()
    fake_client.send_message_raises = _rights_error(err_name)
    with pytest.raises(SendForbiddenError):
        await client.send_text(-100123, "nope")


@pytest.mark.parametrize("err_name", _RIGHTS_ERROR_NAMES)
async def test_send_media_classifies_rights_errors(fake_client, err_name, tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    client = _build(fake_client)
    await client.connect()
    fake_client.send_file_raises = _rights_error(err_name)
    with pytest.raises(SendForbiddenError):
        await client.send_media(-100123, str(f))


@pytest.mark.parametrize("err_name", _RIGHTS_ERROR_NAMES)
async def test_send_reaction_classifies_rights_errors(fake_client, err_name):
    client = _build(fake_client)
    await client.connect()
    fake_client.call_raises = _rights_error(err_name)
    with pytest.raises(SendForbiddenError):
        await client.send_reaction(-100123, 5, "👍")


async def test_slowmode_is_not_classified_as_send_forbidden(fake_client):
    # slow-mode is a transient wait, NOT a read-only state — it must NOT be folded
    # into SendForbiddenError (that would mislabel a writable chat as read-only).
    from telethon.errors import SlowModeWaitError

    client = _build(fake_client)
    await client.connect()
    fake_client.send_message_raises = SlowModeWaitError(request=None)
    with pytest.raises(SlowModeWaitError):
        await client.send_text(-100123, "too fast")


async def test_unknown_forbidden_subclass_is_classified(fake_client):
    # The whole point of category-based classification (#88): a NEW Telethon
    # *ForbiddenError we never hard-listed must still be reclassified — no more
    # "hole in the manual list" (that bug was found three times in #85).
    from telethon.errors import ForbiddenError

    class FutureSendForbiddenError(ForbiddenError):
        def __init__(self):
            super().__init__(request=None, message="CHAT_SEND_FOO_FORBIDDEN")

    client = _build(fake_client)
    await client.connect()
    fake_client.send_message_raises = FutureSendForbiddenError()
    with pytest.raises(SendForbiddenError):
        await client.send_text(-100123, "nope")


async def test_unrelated_badrequest_is_not_classified(fake_client):
    # The category must not be too wide: a BadRequestError that is NOT one of the
    # explicit read-only ones must propagate, not masquerade as read-only.
    from telethon.errors import MessageEmptyError  # a BadRequestError, unrelated to rights

    client = _build(fake_client)
    await client.connect()
    fake_client.send_message_raises = MessageEmptyError(request=None)
    with pytest.raises(MessageEmptyError):
        await client.send_text(-100123, "")


# --- #92: surface the real Telegram reason (clean text), not a fixed line ---


def test_forbidden_message_strips_caused_by_suffix():
    assert (
        _forbidden_message(ValueError("You can't write in this chat (caused by NoneType)"))
        == "You can't write in this chat"
    )
    assert (
        _forbidden_message(
            ValueError("A premium account is required to execute this action (caused by Foo)")
        )
        == "A premium account is required to execute this action"
    )


def test_forbidden_message_falls_back_to_read_only_on_empty():
    assert _forbidden_message(ValueError("")) == READ_ONLY_MESSAGE
    assert _forbidden_message(ValueError("(caused by NoneType)")) == READ_ONLY_MESSAGE


async def test_send_text_forbidden_carries_clean_message(fake_client):
    # the raise site cleans Telethon's "(caused by ...)" trailer so the UI shows a
    # human sentence, not the technical artifact.
    from telethon.errors import ChatWriteForbiddenError

    client = _build(fake_client)
    await client.connect()
    fake_client.send_message_raises = ChatWriteForbiddenError(request=None)
    with pytest.raises(SendForbiddenError) as ei:
        await client.send_text(-100123, "nope")
    assert "(caused by" not in str(ei.value)
    assert "write in this chat" in str(ei.value)


async def test_send_media_forbidden_carries_clean_message(fake_client, tmp_path):
    from telethon.errors import ChatSendMediaForbiddenError

    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    client = _build(fake_client)
    await client.connect()
    fake_client.send_file_raises = ChatSendMediaForbiddenError(request=None)
    with pytest.raises(SendForbiddenError) as ei:
        await client.send_media(-100123, str(media))
    assert "(caused by" not in str(ei.value)


async def test_send_reaction_forbidden_carries_clean_message(fake_client):
    from telethon.errors import ChatWriteForbiddenError

    client = _build(fake_client)
    await client.connect()
    fake_client.call_raises = ChatWriteForbiddenError(request=None)
    with pytest.raises(SendForbiddenError) as ei:
        await client.send_reaction(-100123, 5, "👍")
    assert "(caused by" not in str(ei.value)


async def test_send_text_floodwait_still_retries(fake_client, monkeypatch):
    # классификация прав НЕ должна ломать обычный FloodWait-ретрай:
    # первый вызов кидает транзиентный FloodWait(0), второй — успех.
    from tests.conftest import patch_flood_error

    flood_error = patch_flood_error(monkeypatch)
    client = _build(fake_client)
    await client.connect()
    calls = {"n": 0}
    orig = fake_client.send_message

    async def flaky(peer, text, reply_to=None, schedule=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise flood_error(0)
        return await orig(peer, text, reply_to=reply_to, schedule=schedule)

    fake_client.send_message = flaky
    msg = await client.send_text(7, "hi")
    assert calls["n"] == 2
    assert msg.text == "hi"


async def test_history_maps_messages(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    msgs = await client.history(7, limit=10)
    assert all(isinstance(m, Message) for m in msgs)
    # chronological order (oldest first), regardless of Telethon's newest-first
    assert [m.text for m in msgs] == ["hi", "yo"]
    assert msgs[1].out is True


async def test_history_since_is_uncached_and_passes_min_id(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    first = await client.history_since(7, min_id=1, limit=10)
    second = await client.history_since(7, min_id=1, limit=10)
    assert [m.id for m in first] == [2]
    assert [m.id for m in second] == [2]
    assert fake_client.last_min_id == 1
    assert fake_client.iter_messages_calls == 2


# --- цикл 64: серверный поиск сообщений в диалоге ---

async def test_search_messages_passes_query_and_maps(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    results = await client.search_messages(7, "hi", limit=5)
    assert all(isinstance(m, Message) for m in results)
    assert fake_client.last_search == "hi"  # server-side search= was passed through
    assert [m.text for m in results] == ["hi"]  # only the matching message


async def test_search_messages_limit_passed(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.search_messages(7, "", limit=3)
    assert fake_client.iter_messages_calls >= 1


async def test_search_messages_flood_is_handled(fake_client, monkeypatch):
    # search routes through run_with_flood_wait_retry like every other read
    from tests.conftest import patch_flood_error
    from tg_messenger.core.flood import HandledFloodWaitError

    flood_error = patch_flood_error(monkeypatch)
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()

    def boom(*a, **k):
        async def gen():
            raise flood_error(9999)  # non-transient → HandledFloodWaitError
            yield  # pragma: no cover

        return gen()

    fake_client.iter_messages = boom
    with pytest.raises(HandledFloodWaitError):
        await client.search_messages(7, "hi", limit=5)


def test_media_ref_voice_wins_over_document():
    # Telethon: a voice note is a document with voice=True — .voice must be checked first
    doc = FakeDocument(file_name="note.ogg", size=2048)
    raw = FakeMessage(id=1, sender_id=7, voice=doc,
                      file=FakeDocument(file_name="note.ogg", size=2048, mime_type="audio/ogg"))
    ref = StandaloneTelegramClient._to_media_ref(raw)
    assert ref.kind == "voice"
    assert ref.mime_type == "audio/ogg"


def test_media_ref_photo_carries_mime_type():
    raw = FakeMessage(id=1, sender_id=7, photo=object(),
                      file=FakeDocument(mime_type="image/jpeg"))
    ref = StandaloneTelegramClient._to_media_ref(raw)
    assert ref.kind == "photo"
    assert ref.mime_type == "image/jpeg"


def test_media_ref_without_file_has_no_mime_type():
    raw = FakeMessage(id=1, sender_id=7, photo=object())
    ref = StandaloneTelegramClient._to_media_ref(raw)
    assert ref.kind == "photo"
    assert ref.mime_type is None


async def test_send_text_records_and_returns_message(fake_client):
    client = _build(fake_client)
    await client.connect()
    msg = await client.send_text(7, "hello")
    assert fake_client.sent[-1] == {"peer": 7, "text": "hello", "reply_to": None,
                                    "schedule": None}
    assert isinstance(msg, Message)
    assert msg.out is True


async def test_listen_yields_incoming(fake_client):
    client = _build(fake_client)
    await client.connect()

    # Build a fake NewMessage event that points at dialog 7
    event = type("Evt", (), {})()
    event.chat_id = 7
    event.is_private = True
    event.message = FakeMessage(id=50, sender_id=7, text="ping", out=False)

    received = []

    async def consume():
        async for ev in client.listen():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(event)
    await asyncio.wait_for(task, timeout=1)
    assert isinstance(received[0], IncomingEvent)
    assert received[0].message.text == "ping"
    assert received[0].dialog_id == 7


async def test_listen_skips_non_private_chats(fake_client):
    client = _build(fake_client)
    await client.connect()

    group_event = type("Evt", (), {})()
    group_event.chat_id = -100123
    group_event.is_private = False
    group_event.message = FakeMessage(id=51, sender_id=9, text="group noise", out=False)

    dm_event = type("Evt", (), {})()
    dm_event.chat_id = 7
    dm_event.is_private = True
    dm_event.message = FakeMessage(id=52, sender_id=7, text="dm", out=False)

    received = []

    async def consume():
        async for ev in client.listen():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(group_event)
    await fake_client.push_event(dm_event)
    await asyncio.wait_for(task, timeout=1)
    assert [ev.message.text for ev in received] == ["dm"]


async def test_listen_handler_error_is_logged_not_raised(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()

    broken_event = type("Evt", (), {})()
    broken_event.chat_id = 7
    broken_event.is_private = True
    broken_event.message = object()  # no .date -> mapping blows up

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(broken_event)  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a broken incoming event must be logged"
    assert errors[0].exc_info is not None  # traceback recorded


# --- Цикл 27: поток своих сообщений (listen_outgoing) + get_me ---


def _evt(chat_id, *, is_private, message):
    event = type("Evt", (), {})()
    event.chat_id = chat_id
    event.is_private = is_private
    event.message = message
    return event


async def test_listen_outgoing_yields_own_group_messages(fake_client):
    # is_private=False НЕ фильтруется: группы — суть фичи watch
    client = _build(fake_client)
    await client.connect()
    event = _evt(-100123, is_private=False,
                 message=FakeMessage(id=60, sender_id=1, text="моё в группе", out=True))

    received = []

    async def consume():
        async for ev in client.listen_outgoing():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(event)
    await asyncio.wait_for(task, timeout=1)
    assert isinstance(received[0], OutgoingEvent)
    assert received[0].dialog_id == -100123
    assert received[0].message.text == "моё в группе"
    assert received[0].message.out is True


async def test_outgoing_and_incoming_streams_do_not_cross(fake_client):
    client = _build(fake_client)
    await client.connect()
    out_event = _evt(7, is_private=True,
                     message=FakeMessage(id=61, sender_id=1, text="своё", out=True))
    in_event = _evt(7, is_private=True,
                    message=FakeMessage(id=62, sender_id=7, text="чужое", out=False))

    incoming, outgoing = [], []

    async def consume_in():
        async for ev in client.listen():
            incoming.append(ev)
            return

    async def consume_out():
        async for ev in client.listen_outgoing():
            outgoing.append(ev)
            return

    tasks = [asyncio.create_task(consume_in()), asyncio.create_task(consume_out())]
    await asyncio.sleep(0)
    await fake_client.push_event(out_event)
    await fake_client.push_event(in_event)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert [ev.message.text for ev in incoming] == ["чужое"]
    assert [ev.message.text for ev in outgoing] == ["своё"]


async def test_outgoing_handler_error_is_logged_not_raised(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()
    broken = _evt(7, is_private=True, message=type("Brk", (), {"out": True})())  # нет .date

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(broken)  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_get_me_returns_user(fake_client):
    client = _build(fake_client)
    await client.connect()
    me = await client.get_me()
    assert isinstance(me, User)
    assert me.id == 1
    assert me.username == "me"


# --- Цикл 33: listen_all — входящие из всех чатов (вкладка «Группы») ---


async def test_listen_all_yields_group_and_dm(fake_client):
    client = _build(fake_client)
    await client.connect()
    group_event = _evt(-100200, is_private=False,
                       message=FakeMessage(id=80, sender_id=9, text="из группы", out=False))
    dm_event = _evt(7, is_private=True,
                    message=FakeMessage(id=81, sender_id=7, text="из ЛС", out=False))

    received = []

    async def consume():
        async for ev in client.listen_all():
            received.append(ev)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(group_event)
    await fake_client.push_event(dm_event)
    await asyncio.wait_for(task, timeout=1)
    assert [(ev.dialog_id, ev.message.text) for ev in received] == [
        (-100200, "из группы"), (7, "из ЛС"),
    ]


async def test_listen_all_does_not_leak_groups_into_listen(fake_client):
    client = _build(fake_client)
    await client.connect()
    group_event = _evt(-100200, is_private=False,
                       message=FakeMessage(id=82, sender_id=9, text="группа", out=False))
    dm_event = _evt(7, is_private=True,
                    message=FakeMessage(id=83, sender_id=7, text="лс", out=False))

    dm_stream, all_stream = [], []

    async def consume_dm():
        async for ev in client.listen():
            dm_stream.append(ev)
            return

    async def consume_all():
        async for ev in client.listen_all():
            all_stream.append(ev)
            if len(all_stream) == 2:
                return

    tasks = [asyncio.create_task(consume_dm()), asyncio.create_task(consume_all())]
    await asyncio.sleep(0)
    await fake_client.push_event(group_event)
    await fake_client.push_event(dm_event)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert [ev.message.text for ev in dm_stream] == ["лс"]
    assert [ev.message.text for ev in all_stream] == ["группа", "лс"]


async def test_broken_group_event_is_logged_not_raised(fake_client, caplog):
    # групповые события больше не дропаются до try — сбой обязан попасть в лог
    client = _build(fake_client)
    await client.connect()
    broken = _evt(-100200, is_private=False, message=object())  # нет .date

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(broken)  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


# --- Цикл 28: поток удалений (listen_deleted) + entity_title ---


def _deleted_evt(ids, chat_id=None):
    event = type("Del", (), {})()
    event.deleted_ids = list(ids)
    if chat_id is not None:
        event.chat_id = chat_id
    return event


async def test_listen_deleted_supergroup_carries_chat_id(fake_client):
    client = _build(fake_client)
    await client.connect()

    received = []

    async def consume():
        async for ev in client.listen_deleted():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(_deleted_evt([50, 51], chat_id=-1001234567890))
    await asyncio.wait_for(task, timeout=1)
    assert isinstance(received[0], MessagesDeletedEvent)
    assert received[0].chat_id == -1001234567890
    assert received[0].message_ids == [50, 51]


async def test_listen_deleted_private_has_no_chat_id(fake_client):
    # Telegram не сообщает чат для ЛС/малых групп — chat_id остаётся None
    client = _build(fake_client)
    await client.connect()

    received = []

    async def consume():
        async for ev in client.listen_deleted():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(_deleted_evt([60]))
    await asyncio.wait_for(task, timeout=1)
    assert received[0].chat_id is None
    assert received[0].message_ids == [60]


async def test_deleted_event_does_not_leak_into_message_streams(fake_client):
    client = _build(fake_client)
    await client.connect()

    incoming, deleted = [], []

    async def consume_in():
        async for ev in client.listen():
            incoming.append(ev)
            return

    async def consume_del():
        async for ev in client.listen_deleted():
            deleted.append(ev)
            return

    in_task = asyncio.create_task(consume_in())
    del_task = asyncio.create_task(consume_del())
    await asyncio.sleep(0)
    await fake_client.push_event(_deleted_evt([70]))
    dm = _evt(7, is_private=True, message=FakeMessage(id=71, sender_id=7, text="dm", out=False))
    await fake_client.push_event(dm)
    await asyncio.wait_for(asyncio.gather(in_task, del_task), timeout=1)
    assert [ev.message.text for ev in incoming] == ["dm"]  # deleted-событие не утекло
    assert deleted[0].message_ids == [70]


async def test_entity_title_prefers_group_title(fake_client):
    _seed_dm(fake_client)  # содержит FakeChannel(id=100, title="News")
    client = _build(fake_client)
    await client.connect()
    assert await client.entity_title(100) == "News"


async def test_entity_title_falls_back_to_user_name(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    assert await client.entity_title(7) == "Ann"


async def test_download_message_media_by_id(fake_client, tmp_path):
    fake_client.messages[7] = [FakeMessage(id=42, sender_id=7, text=None, media=object())]
    client = _build(fake_client)
    await client.connect()
    dest = tmp_path / "out.bin"
    result = await client.download_message_media(7, 42, dest)
    assert result == str(dest)
    assert fake_client.downloads[-1]["message_id"] == 42


async def test_external_session_writes_no_files(fake_client, session_dir):
    client = _build(
        fake_client, session_name="acc", external_session=VALID_SESSION, session_dir=session_dir
    )
    await client.connect()
    assert list(session_dir.iterdir()) == []


async def test_typing_proxies_to_telethon_action(fake_client):
    client = _build(fake_client)
    async with client.typing(7):
        assert fake_client.actions_active == [(7, "typing")]
    assert fake_client.actions_active == []  # выключился на выходе
    assert fake_client.actions_log == [(7, "typing")]


async def test_typing_enter_failure_is_swallowed_and_logged(fake_client, caplog):
    client = _build(fake_client)

    def broken_action(entity, action):
        raise RuntimeError("entity not found")

    fake_client.action = broken_action
    body_ran = False
    with caplog.at_level(logging.WARNING, logger="tg_messenger.core.client"):
        async with client.typing(7):
            body_ran = True  # тело выполняется несмотря на сбой индикатора
    assert body_ran
    assert any("typing" in r.message for r in caplog.records)


async def test_typing_exit_failure_is_swallowed_and_logged(fake_client, caplog):
    client = _build(fake_client)

    class _BrokenExit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            raise RuntimeError("cancel failed")

    fake_client.action = lambda entity, action: _BrokenExit()
    with caplog.at_level(logging.WARNING, logger="tg_messenger.core.client"):
        async with client.typing(7):
            pass
    assert any("typing" in r.message for r in caplog.records)


async def test_typing_propagates_body_exceptions(fake_client):
    client = _build(fake_client)
    try:
        async with client.typing(7):
            raise RuntimeError("body failed")
    except RuntimeError as exc:
        assert str(exc) == "body failed"
    else:
        raise AssertionError("body exception must propagate")
    assert fake_client.actions_active == []  # индикатор всё равно погашен


# --- Циклы 42–45: TTL-кэш dialogs/history + инвалидация ---


def _clk(t):
    return lambda: t["now"]


async def test_dialogs_cached_second_call_no_network(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.dialogs(dm_only=True)
    await client.dialogs(dm_only=True)
    assert fake_client.iter_dialogs_calls == 1  # second served from cache


async def test_tab_switching_incident_one_network_call(fake_client):
    """The PR #7 incident: dm→groups→dm must hit the wire ONCE, kinds correct."""
    _seed_all_kinds(fake_client)
    client = _build(fake_client)
    await client.connect()
    dm = await client.dialogs(dm_only=True)
    groups = await client.dialogs(dm_only=False)
    dm2 = await client.dialogs(dm_only=True)
    assert fake_client.iter_dialogs_calls == 1
    assert [d.id for d in dm] == [7]
    assert {d.id for d in groups} == {7, 9, -50, -100123, -100200}
    assert [d.id for d in dm2] == [7]


async def test_dialogs_cache_refetches_after_ttl(fake_client):
    _seed_dm(fake_client)
    t = {"now": 0.0}
    client = _build(fake_client, dialogs_ttl=30.0, clock=_clk(t))
    await client.connect()
    await client.dialogs(dm_only=True)
    t["now"] = 31.0
    await client.dialogs(dm_only=True)
    assert fake_client.iter_dialogs_calls == 2


async def test_dialogs_concurrent_clicks_coalesce(fake_client):
    _seed_all_kinds(fake_client)
    client = _build(fake_client)
    await client.connect()
    gate = asyncio.Event()
    orig = fake_client.iter_dialogs

    def gated(*a, **k):
        inner = orig(*a, **k)

        async def gen():
            await gate.wait()
            async for d in inner:
                yield d

        return gen()

    fake_client.iter_dialogs = gated

    async def release():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()

    await asyncio.gather(
        client.dialogs(dm_only=True),
        client.dialogs(dm_only=False),
        release(),
    )
    assert fake_client.iter_dialogs_calls == 1


async def test_dialogs_result_mutation_does_not_corrupt_cache(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    first = await client.dialogs(dm_only=True)
    first.clear()  # consumer mutates the returned list
    second = await client.dialogs(dm_only=True)
    assert len(second) == 2  # cache untouched
    assert fake_client.iter_dialogs_calls == 1


# --- Цикл 43: кэш history() ---


async def test_history_cached_same_key(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 1


async def test_history_different_limit_separate_entry(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.history(7, limit=20)
    assert fake_client.iter_messages_calls == 2


async def test_history_refetches_after_ttl(fake_client):
    _seed_dm(fake_client)
    t = {"now": 0.0}
    client = _build(fake_client, history_ttl=15.0, clock=_clk(t))
    await client.connect()
    await client.history(7, limit=10)
    t["now"] = 16.0
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 2


async def test_history_returns_copy(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    first = await client.history(7, limit=10)
    first.clear()
    second = await client.history(7, limit=10)
    assert len(second) == 2
    assert fake_client.iter_messages_calls == 1


# --- Цикл 44: инвалидация history ---


async def test_send_text_invalidates_peer_history(fake_client):
    _seed_dm(fake_client)
    fake_client.messages[8] = [FakeMessage(id=3, sender_id=8, text="other")]
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.history(8, limit=10)
    await client.send_text(7, "new")
    await client.history(7, limit=10)  # refetch
    await client.history(8, limit=10)  # still cached
    assert fake_client.iter_messages_calls == 3


async def test_send_media_invalidates_peer_history(fake_client, tmp_path):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    f = tmp_path / "x.jpg"
    f.write_bytes(b"x")
    await client.history(7, limit=10)
    await client.send_media(7, str(f))
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 2


async def test_send_media_passes_flags_to_telethon(fake_client, tmp_path):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    f = tmp_path / "note.ogg"
    f.write_bytes(b"x")
    await client.send_media(7, str(f), caption="cap", voice_note=True,
                            video_note=False, force_document=True)
    rec = fake_client.sent[-1]
    assert rec["file"] == str(f)
    assert rec["caption"] == "cap"
    assert rec["voice_note"] is True
    assert rec["video_note"] is False
    assert rec["force_document"] is True


async def test_send_media_missing_path_raises_before_network(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(ValueError, match="file not found"):
        await client.send_media(7, "/no/such/file.jpg")
    assert fake_client.sent == []


async def test_send_media_directory_raises_before_network(fake_client, tmp_path):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(ValueError, match="file not found"):
        await client.send_media(7, str(tmp_path))
    assert fake_client.sent == []


async def test_incoming_event_invalidates_history(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    ev = type("Ev", (), {
        "chat_id": 7,
        "is_private": True,
        "message": FakeMessage(id=10, sender_id=7, text="ping"),
    })()
    await fake_client.push_event(ev)
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 2


async def test_outgoing_event_invalidates_history(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    ev = type("Ev", (), {
        "chat_id": 7,
        "message": FakeMessage(id=11, sender_id=1, text="me", out=True),
    })()
    await fake_client.push_event(ev)
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 2


async def test_deleted_event_with_chat_id_invalidates_that_peer(fake_client):
    _seed_dm(fake_client)
    fake_client.messages[8] = [FakeMessage(id=3, sender_id=8, text="other")]
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.history(8, limit=10)
    ev = type("Ev", (), {"deleted_ids": [5], "chat_id": 7})()
    await fake_client.push_event(ev)
    await client.history(7, limit=10)  # refetch
    await client.history(8, limit=10)  # cached
    assert fake_client.iter_messages_calls == 3


async def test_deleted_event_without_chat_id_invalidates_all_history(fake_client):
    _seed_dm(fake_client)
    fake_client.messages[8] = [FakeMessage(id=3, sender_id=8, text="other")]
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.history(8, limit=10)
    ev = type("Ev", (), {"deleted_ids": [5]})()  # no chat_id
    await fake_client.push_event(ev)
    await client.history(7, limit=10)
    await client.history(8, limit=10)
    assert fake_client.iter_messages_calls == 4  # both refetched


# --- Цикл 45: flood_sleep_threshold=0 ---


async def test_default_factory_disables_silent_flood_sleep():
    from tg_messenger.core.client import _default_factory

    c = _default_factory(StringSession(), 1, "h")
    assert c.flood_sleep_threshold == 0


async def test_dialogs_flood_raises_handled_and_leaves_cache_empty(fake_client, monkeypatch):
    from tests.conftest import patch_flood_error
    from tg_messenger.core.flood import HandledFloodWaitError

    flood_error = patch_flood_error(monkeypatch)
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1

        async def gen():
            raise flood_error(9999)
            yield  # pragma: no cover

        return gen()

    fake_client.iter_dialogs = boom
    with pytest.raises(HandledFloodWaitError):
        await client.dialogs(dm_only=True)
    # failed fetch not cached → a retry hits the wire again
    with pytest.raises(HandledFloodWaitError):
        await client.dialogs(dm_only=True)
    assert calls["n"] == 2


# --- Цикл 72: поток chat-action (listen_chat_actions) ---


async def _collect_one(coro_stream, push, event):
    """Subscribe to a stream, push one event, return the first received item."""
    received = []

    async def consume():
        async for ev in coro_stream():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await push(event)
    await asyncio.wait_for(task, timeout=1)
    return received[0]


async def test_chat_action_join_carries_user(fake_client):
    client = _build(fake_client)
    await client.connect()
    joiner = FakeUser(id=42, first_name="Joiner", username="joiner")
    ev = FakeChatActionEvent(-100123, user_joined=True, user=joiner)
    out = await _collect_one(client.listen_chat_actions, fake_client.push_event, ev)
    assert isinstance(out, ChatActionEvent)
    assert out.kind == "join"
    assert out.dialog_id == -100123
    assert out.user.id == 42 and out.user.username == "joiner"


async def test_chat_action_user_added_is_join_with_actor(fake_client):
    client = _build(fake_client)
    await client.connect()
    added = FakeUser(id=5, first_name="Newbie")
    admin = FakeUser(id=1, first_name="Admin")
    ev = FakeChatActionEvent(-100123, user_added=True, user=added, added_by=admin)
    out = await _collect_one(client.listen_chat_actions, fake_client.push_event, ev)
    assert out.kind == "join"
    assert out.actor.id == 1


async def test_chat_action_kick_and_leave(fake_client):
    client = _build(fake_client)
    await client.connect()
    victim = FakeUser(id=9, first_name="Gone")
    admin = FakeUser(id=1, first_name="Admin")
    kick = FakeChatActionEvent(-100123, user_kicked=True, user=victim, kicked_by=admin)
    out_kick = await _collect_one(client.listen_chat_actions, fake_client.push_event, kick)
    assert out_kick.kind == "kick"
    assert out_kick.actor.id == 1

    leave = FakeChatActionEvent(-100123, user_left=True, user=victim)
    out_leave = await _collect_one(client.listen_chat_actions, fake_client.push_event, leave)
    assert out_leave.kind == "leave"


async def test_chat_action_title_change(fake_client):
    client = _build(fake_client)
    await client.connect()
    ev = FakeChatActionEvent(-100123, new_title="Renamed Group")
    out = await _collect_one(client.listen_chat_actions, fake_client.push_event, ev)
    assert out.kind == "title"
    assert out.raw_text == "Renamed Group"


async def test_chat_action_broken_event_is_logged_not_raised(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()

    # _is_chat_action so it routes to the handler, but chat_id property raises on read
    class Broken:
        _is_chat_action = True

        @property
        def chat_id(self):
            raise RuntimeError("boom")

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(Broken())  # must not raise
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_chat_action_publish_without_subscribers_is_noop(fake_client):
    client = _build(fake_client)
    await client.connect()
    # nobody subscribed — publish must be a silent no-op, not raise
    await fake_client.push_event(FakeChatActionEvent(-100123, user_joined=True))


# --- Цикл 73: поток read-receipt (listen_reads) ---


async def test_message_read_registers_inbox_and_outbox_builders(fake_client):
    client = _build(fake_client)
    await client.connect()
    read_builders = [
        builder for _, builder in fake_client._handlers if isinstance(builder, events.MessageRead)
    ]
    assert [builder.inbox for builder in read_builders] == [False, True]


async def test_message_read_inbox(fake_client):
    client = _build(fake_client)
    await client.connect()
    ev = FakeMessageReadEvent(7, max_id=120, outbox=False)
    out = await _collect_one(client.listen_reads, fake_client.push_event, ev)
    assert isinstance(out, MessageReadEvent)
    assert out.dialog_id == 7
    assert out.max_id == 120
    assert out.outbox is False


async def test_message_read_outbox_means_they_read_ours(fake_client):
    client = _build(fake_client)
    await client.connect()
    ev = FakeMessageReadEvent(-100123, max_id=99, outbox=True)
    out = await _collect_one(client.listen_reads, fake_client.push_event, ev)
    assert out.outbox is True
    assert out.dialog_id == -100123
    assert out.max_id == 99


# --- Цикл 74: поток реакций (listen_reactions) ---


class FakeReaction:
    def __init__(self, emoticon):
        self.emoticon = emoticon


class FakeReactionResult:
    def __init__(self, reaction):
        self.reaction = reaction


class FakeRecentReaction:
    def __init__(self, reaction):
        self.reaction = reaction


class FakeMessageReactions:
    def __init__(self, results=None, recent_reactions=None):
        self.recent_reactions = recent_reactions
        self.results = results


class FakeReactionUpdate:
    """Stand-in for telethon UpdateMessageReactions — peer/msg_id/reactions."""

    def __init__(self, peer, msg_id, reactions):
        self._raw_update = True
        self.peer = peer
        self.msg_id = msg_id
        self.reactions = reactions


async def test_reaction_emoticon_mapped(fake_client):
    client = _build(fake_client)
    await client.connect()
    reactions = FakeMessageReactions(
        results=[FakeReactionResult(FakeReaction("👍"))],
        recent_reactions=[FakeRecentReaction(FakeReaction("❤️"))],
    )
    upd = FakeReactionUpdate(PeerUser(7), msg_id=55, reactions=reactions)
    out = await _collect_one(client.listen_reactions, fake_client.push_event, upd)
    assert isinstance(out, ReactionEvent)
    assert out.message_id == 55
    assert out.emoticon == "❤️"
    assert out.dialog_id == 7


async def test_reaction_aggregate_without_recent_omits_emoticon(fake_client):
    client = _build(fake_client)
    await client.connect()
    reactions = FakeMessageReactions(results=[FakeReactionResult(FakeReaction("👍"))])
    upd = FakeReactionUpdate(PeerUser(7), msg_id=55, reactions=reactions)
    out = await _collect_one(client.listen_reactions, fake_client.push_event, upd)
    assert out.emoticon is None


async def test_reaction_custom_emoji_maps_to_none(fake_client):
    client = _build(fake_client)
    await client.connect()

    class CustomReaction:  # ReactionCustomEmoji — no .emoticon attribute
        document_id = 12345

    reactions = FakeMessageReactions(recent_reactions=[FakeRecentReaction(CustomReaction())])
    upd = FakeReactionUpdate(PeerUser(7), msg_id=56, reactions=reactions)
    out = await _collect_one(client.listen_reactions, fake_client.push_event, upd)
    assert out.emoticon is None


async def test_reaction_removal_publishes_no_phantom_event(fake_client):
    # #125-A1: Telegram fires UpdateMessageReactions on REMOVAL too (recent_reactions AND
    # results both empty). Publishing a None-emoticon event there is indistinguishable from a
    # real custom reaction → UIs render a permanent fake "<custom>". A removal must be SUPPRESSED.
    client = _build(fake_client)
    await client.connect()
    received: list[ReactionEvent] = []

    async def consume():
        async for ev in client.listen_reactions():
            received.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    # both recent_reactions and results empty == the last reaction was removed
    reactions = FakeMessageReactions(results=[], recent_reactions=[])
    await fake_client.push_event(FakeReactionUpdate(PeerUser(7), msg_id=55, reactions=reactions))
    await asyncio.sleep(0)
    # also the all-None shape (no attributes at all) is a removal/no-reaction state
    await fake_client.push_event(FakeReactionUpdate(PeerUser(7), msg_id=55, reactions=None))
    await asyncio.sleep(0)
    task.cancel()
    assert received == []  # no phantom reaction event for a removal


async def test_reaction_custom_still_published_when_results_present(fake_client):
    # #125-A1 guard: a genuine custom/premium reaction (results show a reaction but no readable
    # standard emoticon) must STILL publish with emoticon=None — only a true removal is suppressed.
    client = _build(fake_client)
    await client.connect()

    class CustomReaction:  # ReactionCustomEmoji — no .emoticon attribute
        document_id = 12345

    reactions = FakeMessageReactions(
        results=[FakeReactionResult(CustomReaction())],
        recent_reactions=[FakeRecentReaction(CustomReaction())],
    )
    upd = FakeReactionUpdate(PeerUser(7), msg_id=56, reactions=reactions)
    out = await _collect_one(client.listen_reactions, fake_client.push_event, upd)
    assert out.emoticon is None


async def test_reaction_unknown_structure_is_warned_not_raised(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()

    # _raw_update routes it, the update is non-empty (not a removal) but accessing msg_id
    # explodes → warning, no crash
    class Broken:
        _raw_update = True
        peer = None
        reactions = FakeMessageReactions(results=[FakeReactionResult(FakeReaction("👍"))])

        @property
        def msg_id(self):
            raise RuntimeError("bad update")

    with caplog.at_level("WARNING", logger="tg_messenger.core.client"):
        await fake_client.push_event(Broken())  # must not raise
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings


# --- Цикл 75: album_id (grouped_id) + send_reaction ---


async def test_incoming_event_carries_album_id(fake_client):
    client = _build(fake_client)
    await client.connect()
    ev = type("Evt", (), {})()
    ev.chat_id = 7
    ev.is_private = True
    ev.message = FakeMessage(id=80, sender_id=7, text="part of album", grouped_id=9001)
    out = await _collect_one(client.listen, fake_client.push_event, ev)
    assert isinstance(out, IncomingEvent)
    assert out.album_id == 9001


async def test_incoming_event_without_album_has_none(fake_client):
    client = _build(fake_client)
    await client.connect()
    ev = type("Evt", (), {})()
    ev.chat_id = 7
    ev.is_private = True
    ev.message = FakeMessage(id=81, sender_id=7, text="single")
    out = await _collect_one(client.listen, fake_client.push_event, ev)
    assert out.album_id is None


async def test_send_reaction_sends_request(fake_client):
    from telethon.tl.functions.messages import SendReactionRequest

    client = _build(fake_client)
    await client.connect()
    await client.send_reaction(7, 55, "👍")
    sent = [r for r in fake_client.requests if isinstance(r, SendReactionRequest)]
    assert sent, "send_reaction must issue a SendReactionRequest"
    assert sent[-1].msg_id == 55
    assert sent[-1].reaction[0].emoticon == "👍"


async def test_send_reaction_flood_is_handled(fake_client, monkeypatch):
    from tests.conftest import patch_flood_error
    from tg_messenger.core.flood import HandledFloodWaitError

    flood_error = patch_flood_error(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(request):
        raise flood_error(9999)  # non-transient → HandledFloodWaitError

    fake_client.__call__ = boom
    monkeypatch.setattr(type(fake_client), "__call__", lambda self, req: boom(req))
    with pytest.raises(HandledFloodWaitError):
        await client.send_reaction(7, 55, "👍")


# --- Цикл 77: reply_to в send_text ---


async def test_send_text_reply_to_passed_to_telethon(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.send_text(7, "re", reply_to=42)
    assert fake_client.sent[-1]["reply_to"] == 42


async def test_send_text_no_reply_to_by_default(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.send_text(7, "plain")
    assert fake_client.sent[-1]["reply_to"] is None


async def test_send_text_reply_maps_reply_to_id(fake_client):
    client = _build(fake_client)
    await client.connect()
    msg = await client.send_text(7, "re", reply_to=42)
    assert msg.reply_to_id == 42


async def test_to_message_maps_reply_to_id_from_raw():
    raw = FakeMessage(id=1, sender_id=7, text="hi", reply_to=99)
    msg = StandaloneTelegramClient._to_message(raw, dialog_id=7)
    assert msg.reply_to_id == 99


async def test_to_message_reply_to_id_none_when_absent():
    raw = FakeMessage(id=1, sender_id=7, text="hi")
    msg = StandaloneTelegramClient._to_message(raw, dialog_id=7)
    assert msg.reply_to_id is None


async def test_to_message_maps_sender_from_raw():
    # #108: the author is mapped from the (already-cached) raw.sender — no network call.
    raw = FakeMessage(id=1, sender_id=9, text="hi",
                      sender=FakeUser(id=9, first_name="Bob", last_name="Lee", username="bob"))
    msg = StandaloneTelegramClient._to_message(raw, dialog_id=-100200)
    assert msg.sender is not None
    assert (msg.sender.id, msg.sender.username) == (9, "bob")
    assert (msg.sender.first_name, msg.sender.last_name) == ("Bob", "Lee")


async def test_to_message_sender_none_when_absent():
    raw = FakeMessage(id=1, sender_id=9, text="hi")  # no .sender attached
    msg = StandaloneTelegramClient._to_message(raw, dialog_id=-100200)
    assert msg.sender is None
    assert msg.sender_id == 9  # the bare id is still present


async def test_send_text_reply_invalidates_history(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.send_text(7, "re", reply_to=1)
    await client.history(7, limit=10)  # must refetch
    assert fake_client.iter_messages_calls == 2


# --- цикл 99: schedule= в send_text (серверные отложенные) ---


async def test_send_text_schedule_passed_to_telethon(fake_client):
    from datetime import timedelta

    client = _build(fake_client)
    await client.connect()
    delay = timedelta(hours=2)
    await client.send_text(7, "later", schedule=delay)
    assert fake_client.sent[-1]["schedule"] == delay


async def test_send_text_no_schedule_by_default(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.send_text(7, "now")
    assert fake_client.sent[-1]["schedule"] is None


# --- Цикл 78: forward / edit / delete ---


def _flood_patch(monkeypatch):
    from tests.conftest import patch_flood_error

    return patch_flood_error(monkeypatch)


def _seed_delete_messages(fake_client, peer: int = 7, ids=(1, 2)) -> None:
    fake_client.messages[int(peer)] = [
        FakeMessage(id=mid, sender_id=1, text=f"m{mid}", out=True, peer_id=int(peer))
        for mid in ids
    ]


async def test_forward_calls_telethon_and_returns_messages(fake_client):
    client = _build(fake_client)
    await client.connect()
    result = await client.forward(7, [1, 2], 8)
    assert fake_client.forwarded[-1] == {"to_peer": 8, "message_ids": [1, 2], "from_peer": 7}
    assert all(isinstance(m, Message) for m in result)
    assert len(result) == 2


async def test_forward_invalidates_both_peers(fake_client):
    _seed_dm(fake_client)
    fake_client.messages[8] = [FakeMessage(id=3, sender_id=8, text="other")]
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.history(8, limit=10)
    await client.forward(7, [1], 8)
    await client.history(7, limit=10)  # refetch
    await client.history(8, limit=10)  # refetch
    assert fake_client.iter_messages_calls == 4


async def test_forward_filters_partial_missing_results(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()

    async def partial_forward(to_peer, message_ids, from_peer):
        return [
            FakeMessage(id=101, sender_id=1, text="ok", out=True, peer_id=to_peer),
            None,
        ]

    fake_client.forward_messages = partial_forward
    with caplog.at_level(logging.WARNING):
        result = await client.forward(7, [1, 999], 8)

    assert [m.id for m in result] == [101]
    assert any("forward returned 1 missing message(s)" in record.message for record in caplog.records)


async def test_forward_flood_is_handled(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(to_peer, message_ids, from_peer):
        raise flood_error(9999)

    fake_client.forward_messages = boom
    with pytest.raises(HandledFloodWaitError):
        await client.forward(7, [1], 8)


async def test_edit_text_calls_telethon_and_returns_message(fake_client):
    client = _build(fake_client)
    await client.connect()
    msg = await client.edit_text(7, 5, "edited")
    assert fake_client.edited[-1] == {"peer": 7, "message_id": 5, "text": "edited"}
    assert isinstance(msg, Message)
    assert msg.text == "edited"


async def test_edit_text_invalidates_history(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.edit_text(7, 1, "x")
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 2


async def test_edit_text_flood_is_handled(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(peer, message_id, text):
        raise flood_error(9999)

    fake_client.edit_message = boom
    with pytest.raises(HandledFloodWaitError):
        await client.edit_text(7, 1, "x")


async def test_delete_messages_calls_telethon_with_revoke(fake_client):
    _seed_delete_messages(fake_client)
    client = _build(fake_client)
    await client.connect()
    await client.delete_messages(7, [1, 2])
    assert fake_client.deleted[-1] == {"peer": 7, "message_ids": [1, 2], "revoke": True}


async def test_delete_messages_for_me_passes_revoke_false(fake_client):
    _seed_delete_messages(fake_client, ids=(1,))
    client = _build(fake_client)
    await client.connect()
    await client.delete_messages(7, [1], revoke=False)
    assert fake_client.deleted[-1]["revoke"] is False


async def test_delete_messages_invalidates_history(fake_client):
    _seed_dm(fake_client)
    _seed_delete_messages(fake_client, ids=(1,))
    client = _build(fake_client)
    await client.connect()
    await client.history(7, limit=10)
    await client.delete_messages(7, [1])
    await client.history(7, limit=10)
    assert fake_client.iter_messages_calls == 3  # history + validation + refetch


async def test_delete_messages_flood_is_handled(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    _seed_delete_messages(fake_client, ids=(1,))
    client = _build(fake_client)
    await client.connect()

    async def boom(peer, message_ids, revoke=True):
        raise flood_error(9999)

    fake_client.delete_messages = boom
    with pytest.raises(HandledFloodWaitError):
        await client.delete_messages(7, [1])


async def test_delete_messages_rejects_missing_ids_before_delete(fake_client):
    _seed_delete_messages(fake_client, ids=(1,))
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(MessageDeleteValidationError, match="not found"):
        await client.delete_messages(7, [1, 2])
    assert fake_client.deleted == []


async def test_delete_messages_rejects_messages_from_other_peer(fake_client):
    fake_client.messages[7] = [
        FakeMessage(id=1, sender_id=1, text="wrong", out=True, peer_id=8)
    ]
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(MessageDeleteValidationError, match="belongs to dialog 8"):
        await client.delete_messages(7, [1])
    assert fake_client.deleted == []


async def test_delete_messages_for_me_rejects_channels_before_delete(fake_client):
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(MessageDeleteValidationError, match="not supported"):
        await client.delete_messages(-1000000000123, [1], revoke=False)
    assert fake_client.deleted == []


# --- Цикл 79: mark_read + unread ---


async def test_mark_read_calls_send_read_acknowledge(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.mark_read(7)
    assert fake_client.read_acks == [{"peer": 7, "max_id": None}]


async def test_mark_read_passes_max_id(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.mark_read(7, max_id=42)
    assert fake_client.read_acks == [{"peer": 7, "max_id": 42}]


async def test_mark_read_invalidates_dialogs_cache(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    first = await client.dialogs()
    assert next(d for d in first if d.id == 7).unread == 2
    assert fake_client.iter_dialogs_calls == 1

    fake_client.dialogs[0].unread_count = 0
    await client.mark_read(7)
    second = await client.dialogs()
    assert next(d for d in second if d.id == 7).unread == 0
    assert fake_client.iter_dialogs_calls == 2


async def test_mark_read_invalidates_archived_dialogs_cache(fake_client):
    fake_client.dialogs = [
        FakeDialog(
            FakeUser(id=8, first_name="Old", contact=False),
            name="Old",
            unread_count=3,
            archived=True,
        ),
    ]
    client = _build(fake_client)
    await client.connect()
    first = await client.archived_dialogs()
    assert first[0].unread == 3
    assert fake_client.iter_dialogs_calls == 1

    fake_client.dialogs[0].unread_count = 0
    await client.mark_read(8)
    second = await client.archived_dialogs()
    assert second[0].unread == 0
    assert fake_client.iter_dialogs_calls == 2


async def test_mark_read_flood_is_handled(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(peer, max_id=None):
        raise flood_error(9999)

    fake_client.send_read_acknowledge = boom
    with pytest.raises(HandledFloodWaitError):
        await client.mark_read(7)


async def test_mark_read_flood_keeps_dialogs_cache(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    first = await client.dialogs()
    assert next(d for d in first if d.id == 7).unread == 2

    async def boom(peer, max_id=None):
        raise flood_error(9999)

    fake_client.dialogs[0].unread_count = 0
    fake_client.send_read_acknowledge = boom
    with pytest.raises(HandledFloodWaitError):
        await client.mark_read(7)
    second = await client.dialogs()
    assert next(d for d in second if d.id == 7).unread == 2
    assert fake_client.iter_dialogs_calls == 1


async def test_mark_read_flood_keeps_archived_dialogs_cache(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    fake_client.dialogs = [
        FakeDialog(
            FakeUser(id=8, first_name="Old", contact=False),
            name="Old",
            unread_count=3,
            archived=True,
        ),
    ]
    client = _build(fake_client)
    await client.connect()
    first = await client.archived_dialogs()
    assert first[0].unread == 3

    async def boom(peer, max_id=None):
        raise flood_error(9999)

    fake_client.dialogs[0].unread_count = 0
    fake_client.send_read_acknowledge = boom
    with pytest.raises(HandledFloodWaitError):
        await client.mark_read(8)
    second = await client.archived_dialogs()
    assert second[0].unread == 3
    assert fake_client.iter_dialogs_calls == 1


async def test_dialog_unread_mapped_from_telethon(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs()
    ann = next(d for d in dialogs if d.id == 7)
    assert ann.unread == 2


# --- Цикл 88: mute_user / ban_user (edit_permissions обёртки) ---

async def test_mute_user_restricts_send_messages(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.mute_user(-100200, 7, 300)
    call = fake_client.permissions[-1]
    assert call["entity"] == -100200 and call["user"] == 7
    assert call["rights"] == {
        "send_messages": False,
        "send_media": False,
        "send_stickers": False,
        "send_gifs": False,
        "send_games": False,
        "send_inline": False,
        "send_polls": False,
        "embed_link_previews": False,
    }
    assert call["until_date"] is not None  # muted until a future time


async def test_ban_user_revokes_view_messages(fake_client):
    client = _build(fake_client)
    await client.connect()
    await client.ban_user(-100200, 7)
    call = fake_client.permissions[-1]
    assert call["entity"] == -100200 and call["user"] == 7
    assert call["rights"].get("view_messages") is False


async def test_mute_user_flood_is_handled(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(entity, user=None, until_date=None, **rights):
        raise flood_error(9999)

    fake_client.edit_permissions = boom
    with pytest.raises(HandledFloodWaitError):
        await client.mute_user(-100200, 7, 300)


async def test_ban_user_flood_is_handled(fake_client, monkeypatch):
    from tg_messenger.core.flood import HandledFloodWaitError
    flood_error = _flood_patch(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(entity, user=None, until_date=None, **rights):
        raise flood_error(9999)

    fake_client.edit_permissions = boom
    with pytest.raises(HandledFloodWaitError):
        await client.ban_user(-100200, 7)


# --- Цикл 89: is_admin (проверка прав модератора на старте) ---

async def test_is_admin_true_when_can_delete(fake_client):
    class Perms:
        is_admin = True
        delete_messages = True

    seen = {}

    async def perms(entity, user=None):
        seen["entity"] = entity
        seen["user"] = user
        return Perms()

    fake_client.get_permissions = perms
    client = _build(fake_client)
    await client.connect()
    assert await client.is_admin(-100200) is True
    assert seen["entity"] == -100200
    assert getattr(seen["user"], "id", None) == 1


async def test_is_admin_false_when_not_admin(fake_client):
    class Perms:
        is_admin = True
        delete_messages = False
        ban_users = False

    async def perms(entity, user=None):
        return Perms()

    fake_client.get_permissions = perms
    client = _build(fake_client)
    await client.connect()
    assert await client.is_admin(-100200) is False


async def test_is_admin_true_when_can_ban(fake_client):
    class Perms:
        is_admin = True
        delete_messages = False
        ban_users = True

    async def perms(entity, user=None):
        return Perms()

    fake_client.get_permissions = perms
    client = _build(fake_client)
    await client.connect()
    assert await client.is_admin(-100200) is True


async def test_is_admin_false_on_error(fake_client):
    async def boom(entity, user=None):
        raise RuntimeError("not a participant")

    fake_client.get_permissions = boom
    client = _build(fake_client)
    await client.connect()
    assert await client.is_admin(-100200) is False


async def test_log_out_calls_telethon(fake_client):
    # #11 (комментарий): logout — best-effort log_out() перед удалением файла
    client = _build(fake_client)
    await client.connect()
    assert await client.log_out() is True
    assert fake_client.logged_out is True


# --- Цикл 120: check/set/clear username ---


async def test_check_username_free_returns_true(fake_client):
    client = _build(fake_client)
    await client.connect()
    assert await client.check_username("freename1") is True


async def test_check_username_occupied_returns_false(fake_client):
    fake_client.occupied_usernames.add("takenname")
    client = _build(fake_client)
    await client.connect()
    assert await client.check_username("takenname") is False


async def test_check_username_invalid_raises_value_error(fake_client):
    fake_client.invalid_usernames.add("bad!")
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(ValueError):
        await client.check_username("bad!")


async def test_check_username_issues_request(fake_client):
    from telethon.tl.functions.account import CheckUsernameRequest

    client = _build(fake_client)
    await client.connect()
    await client.check_username("freename1")
    reqs = [r for r in fake_client.requests if isinstance(r, CheckUsernameRequest)]
    assert reqs and reqs[-1].username == "freename1"


async def test_set_username_writes(fake_client):
    from telethon.tl.functions.account import UpdateUsernameRequest

    client = _build(fake_client)
    await client.connect()
    await client.set_username("mynewname")
    assert fake_client.set_username_to == "mynewname"
    reqs = [r for r in fake_client.requests if isinstance(r, UpdateUsernameRequest)]
    assert reqs and reqs[-1].username == "mynewname"


async def test_set_username_occupied_raises_value_error(fake_client):
    fake_client.occupied_usernames.add("takenname")
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(ValueError):
        await client.set_username("takenname")


async def test_set_username_invalid_raises_value_error(fake_client):
    fake_client.invalid_usernames.add("bad!")
    client = _build(fake_client)
    await client.connect()
    with pytest.raises(ValueError):
        await client.set_username("bad!")


async def test_clear_username_sends_empty(fake_client):
    from telethon.tl.functions.account import UpdateUsernameRequest

    client = _build(fake_client)
    await client.connect()
    await client.clear_username()
    reqs = [r for r in fake_client.requests if isinstance(r, UpdateUsernameRequest)]
    assert reqs and reqs[-1].username == ""
    assert fake_client.set_username_to == ""


async def test_check_username_flood_is_handled(fake_client, monkeypatch):
    from tests.conftest import patch_flood_error
    from tg_messenger.core.flood import HandledFloodWaitError

    flood_error = patch_flood_error(monkeypatch)
    client = _build(fake_client)
    await client.connect()

    async def boom(request):
        raise flood_error(9999)

    monkeypatch.setattr(type(fake_client), "__call__", lambda self, req: boom(req))
    with pytest.raises(HandledFloodWaitError):
        await client.check_username("freename1")


# --- цикл 128: интеграция token-bucket в отправку (#25) ---

async def test_send_rate_limit_waits_when_exhausted(fake_client, caplog):
    _seed_dm(fake_client)
    t = {"now": 0.0}
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        t["now"] += s

    # rate 60/min = 1/sec, burst 1: first send instant, second must wait ~1s.
    # swap in a burst=1 bucket with the fake clock+sleep (the default burst = 1 minute).
    from tg_messenger.core.ratelimit import TokenBucket

    client = _build(fake_client)
    client._send_bucket = TokenBucket(60.0, burst=1, clock=lambda: t["now"], sleep=fake_sleep)
    await client.connect()
    with caplog.at_level("WARNING", logger="tg_messenger.core.ratelimit"):
        await client.send_text(7, "first")
        await client.send_text(7, "second")
    assert len(slept) == 1
    assert abs(slept[0] - 1.0) < 0.01
    assert any("rate limit" in r.message for r in caplog.records)


async def test_send_rate_limit_enabled_by_default(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    assert client._send_bucket.enabled is True
    assert client._send_bucket._rate_per_sec == pytest.approx(20 / 60)


async def test_send_rate_limit_zero_explicitly_disables(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client, send_rate_per_min=0)
    await client.connect()
    assert client._send_bucket.enabled is False
    for i in range(50):
        await client.send_text(7, f"msg{i}")  # never blocks
    assert len(fake_client.sent) == 50


def test_client_from_env_defaults_send_rate_to_20(monkeypatch):
    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.delenv("TG_SEND_RATE", raising=False)
    monkeypatch.setattr(client_module, "StandaloneTelegramClient", FakeStandaloneTelegramClient)

    client_module.client_from_env()

    assert captured["send_rate_per_min"] == 20.0


def test_client_from_env_allows_explicit_zero_send_rate(monkeypatch):
    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SEND_RATE", "0")
    monkeypatch.setattr(client_module, "StandaloneTelegramClient", FakeStandaloneTelegramClient)

    client_module.client_from_env()

    assert captured["send_rate_per_min"] == 0.0


def test_client_from_env_uses_configured_send_rate(monkeypatch):
    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SEND_RATE", "20")
    monkeypatch.setattr(client_module, "StandaloneTelegramClient", FakeStandaloneTelegramClient)

    client_module.client_from_env()

    assert captured["send_rate_per_min"] == 20.0


def test_client_from_env_explicit_session_dir_skips_env_resolution(monkeypatch, tmp_path):
    # An explicit session_dir must short-circuit the default: client_from_env must NOT
    # evaluate resolve_env_dir("TG_SESSION_DIR")/default_session_dir() when the caller
    # supplied one — otherwise a bad TG_SESSION_DIR (or bad TG_HOME) would raise even
    # though the caller never relies on either. Both are set to invalid RELATIVE values
    # here to prove neither is touched.
    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_DIR", "relative-bad")  # would raise if evaluated
    monkeypatch.setenv("TG_HOME", "also-relative-bad")    # would raise via default_session_dir
    monkeypatch.setattr(client_module, "StandaloneTelegramClient", FakeStandaloneTelegramClient)

    explicit = tmp_path / "my-sessions"
    # must not raise despite both bad env values, and must use the passed dir verbatim
    client_module.client_from_env(session_dir=explicit)

    assert captured["session_dir"] == explicit
