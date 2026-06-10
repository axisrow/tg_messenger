from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tg_messenger.core.models import Dialog, IncomingEvent, MediaRef, Message, User


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
