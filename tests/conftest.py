"""Shared test fixtures: a network-free fake Telethon client."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telethon import events as _tg_events


def _builder_matches(builder, event) -> bool:
    """Mimic Telethon dispatch: route a pushed event only to matching builders.

    Just enough for our handlers: MessageDeleted events are recognised by the
    ``deleted_ids`` attribute; NewMessage(incoming=)/(outgoing=) — by message.out.
    """
    if builder is None:
        return True
    if isinstance(builder, _tg_events.MessageDeleted):
        return hasattr(event, "deleted_ids")
    if isinstance(builder, _tg_events.NewMessage):
        if hasattr(event, "deleted_ids"):
            return False
        out = bool(getattr(getattr(event, "message", None), "out", False))
        if builder.outgoing and not builder.incoming:
            return out
        if builder.incoming and not builder.outgoing:
            return not out
    return True


class FakeUser:
    def __init__(self, id, first_name=None, last_name=None, username=None, bot=False):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.bot = bot


class FakeChannel:
    def __init__(self, id, title=None, username=None):
        self.id = id
        self.title = title
        self.username = username


class FakeDocument:
    def __init__(self, file_name=None, size=None, mime_type=None):
        self.file_name = file_name
        self.size = size
        self.mime_type = mime_type


class FakeMessage:
    def __init__(
        self, id, sender_id, text=None, out=False, date=None, media=None, peer_id=None,
        photo=None, document=None, voice=None, file=None,
    ):
        self.id = id
        self.sender_id = sender_id
        self.message = text
        self.text = text
        self.out = out
        self.date = date or datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        # Telethon exposes .media plus convenience .photo/.document/.voice/.file
        self.media = media if media is not None else (photo or document or voice)
        self.photo = photo
        self.document = document if document is not None else voice  # a voice note IS a document
        self.voice = voice
        self.file = file
        self.peer_id = peer_id


class FakeDialog:
    def __init__(self, entity, *, name="", unread_count=0, message=None):
        self.entity = entity
        self.id = entity.id
        self.name = name
        self.title = name
        self.unread_count = unread_count
        self.message = message


def make_sent_code(kind: str = "App", phone_code_hash: str = "hash123",
                   next_kind: str | None = None):
    """Fake auth.SentCode: type/next_type class names mimic telethon's (SentCodeTypeApp...)."""
    attrs = {"phone_code_hash": phone_code_hash, "type": type(f"SentCodeType{kind}", (), {})()}
    if next_kind is not None:
        attrs["next_type"] = type(f"CodeType{next_kind}", (), {})()
    return type("Sent", (), attrs)()


class FakeTelethonClient:
    """Drop-in stand-in for telethon.TelegramClient — no network.

    Records sends, yields canned dialogs/messages, and can push a fake
    NewMessage event into the registered handler.
    """

    def __init__(self, *args, **kwargs):
        self.connected = False
        self._authorized = True
        self.dialogs: list[FakeDialog] = []
        self.messages: dict[int, list[FakeMessage]] = {}
        self.sent: list[dict] = []
        self.downloads: list[dict] = []
        self.actions_active: list[tuple] = []
        self.actions_log: list[tuple] = []
        self._handlers: list = []
        self.code_requests: list[str] = []
        self.resend_requests: list = []
        self.signed_in_with: list = []

    # --- connection / auth ---
    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self._authorized

    async def send_code_request(self, phone):
        self.code_requests.append(phone)
        # mimic telethon: SentCode.type tells where the code went (SentCodeTypeApp etc.)
        return make_sent_code("App", "hash123")

    async def __call__(self, request):
        # raw RPC path; LoginFlow uses it for auth.ResendCodeRequest
        self.resend_requests.append(request)
        return make_sent_code("Sms", "hash456")

    async def sign_in(self, phone=None, code=None, password=None, **kw):
        self.signed_in_with.append({"phone": phone, "code": code, "password": password})
        self._authorized = True
        return FakeUser(id=1, first_name="Me")

    async def get_me(self):
        return FakeUser(id=1, first_name="Me", username="me")

    # --- dialogs / history ---
    async def get_dialogs(self, *a, **k):
        return self.dialogs

    def iter_dialogs(self, *a, **k):
        async def gen():
            for d in self.dialogs:
                yield d

        return gen()

    def iter_messages(self, peer, limit=50, ids=None, **k):
        items = self.messages.get(int(peer), [])
        if ids is not None:
            wanted = ids if isinstance(ids, (list, tuple)) else [ids]
            items = [m for m in items if m.id in wanted]
        else:
            items = items[:limit]

        async def gen():
            for m in items:
                yield m

        return gen()

    async def get_entity(self, peer):
        for d in self.dialogs:
            if d.entity.id == int(peer):
                return d.entity
        return FakeUser(id=int(peer))

    # --- sending ---
    async def send_message(self, peer, text):
        msg = FakeMessage(id=999, sender_id=1, text=text, out=True, peer_id=int(peer))
        self.sent.append({"peer": int(peer), "text": text})
        return msg

    async def send_file(self, peer, file, caption=None):
        msg = FakeMessage(id=998, sender_id=1, text=caption, out=True, peer_id=int(peer))
        self.sent.append({"peer": int(peer), "file": str(file), "caption": caption})
        return msg

    async def download_media(self, message, file):
        self.downloads.append({"message_id": getattr(message, "id", None), "dest": str(file)})
        return str(file)

    # --- chat actions (typing indicator) ---
    def action(self, entity, action):
        fake = self

        class _Action:
            async def __aenter__(self):
                fake.actions_active.append((int(entity), action))
                fake.actions_log.append((int(entity), action))
                return self

            async def __aexit__(self, *exc):
                fake.actions_active.remove((int(entity), action))
                return False

        return _Action()

    # --- events ---
    def add_event_handler(self, handler, event=None):
        self._handlers.append((handler, event))

    async def push_event(self, event):
        for handler, builder in self._handlers:
            if _builder_matches(builder, event):
                await handler(event)


@pytest.fixture(autouse=True)
def _isolated_log_dir(tmp_path, monkeypatch):
    # the CLI entrypoint calls setup_logging(); tests must never write ~/.tg_messenger/logs
    monkeypatch.setenv("TG_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def fake_client():
    return FakeTelethonClient()


@pytest.fixture
def session_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d
