from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tg_messenger.core.models import (
    Dialog,
    IncomingEvent,
    MediaRef,
    Message,
    MessagesDeletedEvent,
    OutgoingEvent,
    User,
)


def _now():
    return datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_user_minimal():
    user = User(id=42, first_name="Ann")
    assert user.id == 42
    assert user.first_name == "Ann"
    assert user.username is None


def test_dialog_fields():
    dialog = Dialog(id=7, title="Ann", username="ann", unread=3, last_message_at=_now())
    assert dialog.id == 7
    assert dialog.unread == 3
    assert dialog.username == "ann"


def test_dialog_defaults():
    dialog = Dialog(id=7, title="Ann")
    assert dialog.unread == 0
    assert dialog.username is None
    assert dialog.last_message_at is None


def test_dialog_kind_defaults_to_dm():
    assert Dialog(id=7, title="Ann").kind == "dm"


def test_dialog_kind_accepts_group_channel_bot():
    assert Dialog(id=-50, title="Devs", kind="group").kind == "group"
    assert Dialog(id=-100123, title="News", kind="channel").kind == "channel"
    assert Dialog(id=9, title="HelperBot", kind="bot").kind == "bot"


def test_dialog_kind_rejects_unknown():
    with pytest.raises(ValidationError):
        Dialog(id=7, title="x", kind="megagroup")


def test_message_text_only():
    msg = Message(id=1, dialog_id=7, sender_id=42, out=False, text="hi", date=_now())
    assert msg.text == "hi"
    assert msg.out is False
    assert msg.media is None


def test_message_with_media():
    media = MediaRef(kind="photo", file_name="p.jpg", size=1024, downloadable=True)
    msg = Message(id=2, dialog_id=7, sender_id=42, out=True, date=_now(), media=media)
    assert msg.text is None
    assert msg.media.kind == "photo"
    assert msg.media.downloadable is True


def test_media_kind_constrained():
    with pytest.raises(ValidationError):
        MediaRef(kind="banana", downloadable=False)


def test_media_ref_voice_kind_and_mime():
    media = MediaRef(kind="voice", mime_type="audio/ogg")
    assert media.kind == "voice"
    assert media.mime_type == "audio/ogg"


def test_media_ref_mime_defaults_to_none():
    assert MediaRef(kind="photo").mime_type is None


def test_incoming_event_wraps_message():
    msg = Message(id=3, dialog_id=7, sender_id=42, out=False, text="yo", date=_now())
    event = IncomingEvent(dialog_id=7, message=msg)
    assert event.dialog_id == 7
    assert event.message.text == "yo"


def test_message_requires_date():
    with pytest.raises(ValidationError):
        Message(id=1, dialog_id=7, sender_id=42, out=False, text="hi")


def test_outgoing_event_wraps_message():
    msg = Message(id=3, dialog_id=-100123, sender_id=1, out=True, text="моё", date=_now())
    event = OutgoingEvent(dialog_id=-100123, message=msg)
    assert event.dialog_id == -100123
    assert event.message.out is True


def test_messages_deleted_event_chat_unknown_by_default():
    # в ЛС/малых группах Telegram не сообщает чат — только id сообщений
    event = MessagesDeletedEvent(message_ids=[50, 51])
    assert event.chat_id is None
    assert event.message_ids == [50, 51]


def test_messages_deleted_event_with_supergroup_chat():
    event = MessagesDeletedEvent(chat_id=-1001234567890, message_ids=[7])
    assert event.chat_id == -1001234567890


def test_messages_deleted_event_rejects_garbage():
    with pytest.raises(ValidationError):
        MessagesDeletedEvent(message_ids="not-a-list")


# --- цикл 71: новые event-модели (#14) ---

def test_chat_action_event_defaults():
    from tg_messenger.core.models import ChatActionEvent

    ev = ChatActionEvent(dialog_id=-100200, kind="join")
    assert ev.dialog_id == -100200
    assert ev.kind == "join"
    assert ev.user is None and ev.actor is None and ev.raw_text is None


def test_chat_action_event_with_users():
    from tg_messenger.core.models import ChatActionEvent

    u = User(id=7, first_name="Ann")
    a = User(id=1, first_name="Admin")
    ev = ChatActionEvent(dialog_id=-100200, kind="kick", user=u, actor=a, raw_text="kicked")
    assert ev.user.id == 7 and ev.actor.id == 1 and ev.raw_text == "kicked"


def test_chat_action_kind_rejects_unknown():
    from tg_messenger.core.models import ChatActionEvent

    with pytest.raises(ValidationError):
        ChatActionEvent(dialog_id=1, kind="bogus")


def test_message_read_event():
    from tg_messenger.core.models import MessageReadEvent

    ev = MessageReadEvent(dialog_id=7, max_id=42, outbox=True)
    assert ev.dialog_id == 7 and ev.max_id == 42 and ev.outbox is True


def test_message_read_event_outbox_defaults_false():
    from tg_messenger.core.models import MessageReadEvent

    assert MessageReadEvent(dialog_id=7, max_id=1).outbox is False


def test_reaction_event_defaults():
    from tg_messenger.core.models import ReactionEvent

    ev = ReactionEvent(dialog_id=7, message_id=10)
    assert ev.emoticon is None and ev.actor_id is None


def test_reaction_event_with_emoticon():
    from tg_messenger.core.models import ReactionEvent

    ev = ReactionEvent(dialog_id=7, message_id=10, emoticon="👍", actor_id=99)
    assert ev.emoticon == "👍" and ev.actor_id == 99


def test_incoming_event_album_id_defaults_none():
    msg = Message(id=1, dialog_id=7, sender_id=7, out=False, text="x", date=_now())
    ev = IncomingEvent(dialog_id=7, message=msg)
    assert ev.album_id is None


def test_incoming_event_album_id_set():
    msg = Message(id=1, dialog_id=7, sender_id=7, out=False, text="x", date=_now())
    ev = IncomingEvent(dialog_id=7, message=msg, album_id=555)
    assert ev.album_id == 555
