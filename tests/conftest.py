"""Shared test fixtures: a network-free fake Telethon client."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

import pytest
from telethon import events as _tg_events


def _builder_matches(builder, event) -> bool:
    """Mimic Telethon dispatch: route a pushed event only to matching builders.

    Just enough for our handlers: MessageDeleted events are recognised by the
    ``deleted_ids`` attribute; NewMessage(incoming=)/(outgoing=) — by message.out;
    ChatAction by the ``_is_chat_action`` marker; MessageRead by ``_is_message_read``;
    Raw (reactions) by the ``_raw_update`` marker.
    """
    if builder is None:
        return True
    if isinstance(builder, _tg_events.Raw):
        # events.Raw(UpdateMessageReactions) — route fake raw updates here
        return getattr(event, "_raw_update", False)
    if isinstance(builder, _tg_events.MessageDeleted):
        return hasattr(event, "deleted_ids")
    if isinstance(builder, _tg_events.ChatAction):
        return getattr(event, "_is_chat_action", False)
    if isinstance(builder, _tg_events.MessageRead):
        return (
            getattr(event, "_is_message_read", False)
            and bool(getattr(builder, "inbox", False)) != bool(getattr(event, "outbox", False))
        )
    if isinstance(builder, _tg_events.NewMessage):
        if hasattr(event, "deleted_ids"):
            return False
        if (
            getattr(event, "_is_chat_action", False)
            or getattr(event, "_is_message_read", False)
            or getattr(event, "_raw_update", False)
        ):
            return False
        out = bool(getattr(getattr(event, "message", None), "out", False))
        if builder.outgoing and not builder.incoming:
            return out
        if builder.incoming and not builder.outgoing:
            return not out
    return True


class FakeFloodWaitError(Exception):
    """Mimics telethon.errors.FloodWaitError (.seconds attribute)."""

    def __init__(self, seconds):
        super().__init__(f"flood {seconds}s")
        self.seconds = seconds


def patch_flood_error(monkeypatch) -> type[FakeFloodWaitError]:
    """Point core.flood at the fake FloodWaitError; returns the class to raise."""
    import tg_messenger.core.flood as flood

    monkeypatch.setattr(flood, "FloodWaitError", FakeFloodWaitError)
    return FakeFloodWaitError


class FakeUser:
    def __init__(
        self,
        id,
        first_name=None,
        last_name=None,
        username=None,
        bot=False,
        contact=False,
        lang_code=None,
    ):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.bot = bot
        self.contact = contact
        self.lang_code = lang_code


class FakeBannedRights:
    """Telethon ChatBannedRights — True means RESTRICTED (the raw MTProto flag)."""

    def __init__(self, send_messages=False):
        self.send_messages = send_messages


class FakeAdminRights:
    """Telethon ChatAdminRights — presence on an entity means the account is an admin."""

    def __init__(self, post_messages=True):
        self.post_messages = post_messages


class FakeChannel:
    """Telethon Channel: supergroup (broadcast=False) or broadcast channel (True).

    creator/admin_rights/default_banned_rights/banned_rights/left default to the
    writable shape (an ordinary subscriber of a broadcast is still read-only) — they
    are the inputs for the can_send capability computed in _fetch_dialogs.
    """

    def __init__(self, id, title=None, username=None, broadcast=False, *,
                 creator=False, admin_rights=None, default_banned_rights=None,
                 banned_rights=None, left=False):
        self.id = id
        self.title = title
        self.username = username
        self.broadcast = broadcast
        self.creator = creator
        self.admin_rights = admin_rights
        self.default_banned_rights = default_banned_rights
        self.banned_rights = banned_rights
        self.left = left


class FakeChat:
    """Telethon Chat (small group) — carries a title and NO broadcast attribute."""

    def __init__(self, id, title=None, username=None, *,
                 creator=False, admin_rights=None, default_banned_rights=None,
                 banned_rights=None, left=False):
        self.id = id
        self.title = title
        self.username = username
        self.creator = creator
        self.admin_rights = admin_rights
        self.default_banned_rights = default_banned_rights
        self.banned_rights = banned_rights
        self.left = left


class FakeDocument:
    def __init__(self, file_name=None, size=None, mime_type=None):
        self.file_name = file_name
        self.size = size
        self.mime_type = mime_type


class FakeReplyTo:
    """Telethon message.reply_to header — only reply_to_msg_id is read."""

    def __init__(self, reply_to_msg_id):
        self.reply_to_msg_id = reply_to_msg_id


class FakeMessage:
    def __init__(
        self, id, sender_id, text=None, out=False, date=None, media=None, peer_id=None,
        photo=None, document=None, voice=None, file=None, grouped_id=None, reply_to=None,
        sender=None,
    ):
        self.id = id
        self.grouped_id = grouped_id
        self.sender_id = sender_id
        # Telethon attaches the resolved sender entity (User) best-effort; None when uncached.
        self.sender = sender
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
        # Telethon: .reply_to is a MessageReplyHeader (or None); .reply_to_msg_id on it
        self.reply_to = FakeReplyTo(reply_to) if reply_to is not None else None


def _marked_id(entity) -> int:
    """Mimic telethon Dialog.id (utils.get_peer_id): marked id, negative for groups/channels."""
    if isinstance(entity, FakeChannel):
        return int(f"-100{entity.id}")
    if isinstance(entity, FakeChat):
        return -entity.id
    return entity.id


class FakeDialog:
    def __init__(self, entity, *, name="", unread_count=0, message=None, archived=False):
        self.entity = entity
        self.id = _marked_id(entity)
        self.name = name
        self.title = name
        self.unread_count = unread_count
        self.message = message
        self.folder_id = 1 if archived else None


class FakeChatActionEvent:
    """Stand-in for telethon events.ChatAction.Event — only the attrs our handler reads.

    Mirrors the real flag set: user_joined/user_added/user_left/user_kicked/new_title/
    new_pin/new_photo, plus user/added_by/kicked_by and action_message (for raw_text).
    """

    def __init__(
        self, chat_id, *, user_joined=False, user_added=False, user_left=False,
        user_kicked=False, new_title=None, new_pin=False, new_photo=False,
        user=None, added_by=None, kicked_by=None, action_message=None,
    ):
        self._is_chat_action = True
        self.chat_id = chat_id
        self.user_joined = user_joined
        self.user_added = user_added
        self.user_left = user_left
        self.user_kicked = user_kicked
        self.new_title = new_title
        self.new_pin = new_pin
        self.new_photo = new_photo
        self.user = user
        self.added_by = added_by
        self.kicked_by = kicked_by
        self.action_message = action_message


class FakeMessageReadEvent:
    """Stand-in for telethon events.MessageRead.Event — chat_id/max_id/outbox."""

    def __init__(self, chat_id, max_id, outbox=False):
        self._is_message_read = True
        self.chat_id = chat_id
        self.max_id = max_id
        self.outbox = outbox


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
        self.forwarded: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[dict] = []
        self.read_acks: list[dict] = []
        self.permissions: list[dict] = []  # edit_permissions calls (mute/ban)
        self.downloads: list[dict] = []
        self.actions_active: list[tuple] = []
        self.actions_log: list[tuple] = []
        self._handlers: list = []
        self.code_requests: list[str] = []
        self.requests: list = []  # every raw RPC call (reactions, resend, ...)
        self.resend_requests: list = []
        # username RPCs: CheckUsernameRequest / UpdateUsernameRequest
        self.occupied_usernames: set[str] = set()
        self.invalid_usernames: set[str] = set()
        self.set_username_to: str | None = None
        self.signed_in_with: list = []
        # network-call counters: the TTL-cache tests assert how often we hit the wire
        self.iter_dialogs_calls = 0
        self.iter_messages_calls = 0
        self.last_min_id = None
        # optional: make a send raise this (rights-error classification tests)
        self.send_message_raises: BaseException | None = None
        self.send_file_raises: BaseException | None = None
        self.call_raises: BaseException | None = None  # raw __call__ (reactions)

    # --- connection / auth ---
    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self._authorized

    async def log_out(self):
        self.logged_out = True
        self._authorized = False
        return True

    async def send_code_request(self, phone):
        self.code_requests.append(phone)
        # mimic telethon: SentCode.type tells where the code went (SentCodeTypeApp etc.)
        return make_sent_code("App", "hash123")

    async def __call__(self, request):
        # raw RPC path; LoginFlow uses it for auth.ResendCodeRequest, and the
        # client uses it for SendReactionRequest etc. — record every raw call.
        self.requests.append(request)
        self.resend_requests.append(request)
        if self.call_raises is not None:
            raise self.call_raises
        # username RPCs: dispatch by class name (avoid importing telethon types here)
        cls = type(request).__name__
        if cls == "CheckUsernameRequest":
            uname = getattr(request, "username", "")
            if uname in self.invalid_usernames:
                from telethon.errors import UsernameInvalidError
                raise UsernameInvalidError(request=request)
            return uname not in self.occupied_usernames
        if cls == "UpdateUsernameRequest":
            uname = getattr(request, "username", "")
            if uname in self.invalid_usernames:
                from telethon.errors import UsernameInvalidError
                raise UsernameInvalidError(request=request)
            if uname and uname in self.occupied_usernames:
                from telethon.errors import UsernameOccupiedError
                raise UsernameOccupiedError(request=request)
            self.set_username_to = uname
            return FakeUser(id=1, first_name="Me", username=uname or None)
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
        self.iter_dialogs_calls += 1
        archived = bool(k.get("archived", False))

        async def gen():
            for d in self.dialogs:
                if bool(getattr(d, "folder_id", None) == 1) == archived:
                    yield d

        return gen()

    def iter_messages(self, peer, limit=50, ids=None, search=None, min_id=0, **k):
        self.iter_messages_calls += 1
        self.last_min_id = min_id
        self.last_search = search  # so search_messages tests can assert it was passed
        items = self.messages.get(int(peer), [])
        if ids is not None:
            wanted = ids if isinstance(ids, (list, tuple)) else [ids]
            items = [m for m in items if m.id in wanted]
        else:
            if search is not None:
                items = [m for m in items if search.casefold() in (m.text or "").casefold()]
            if min_id:
                items = [m for m in items if m.id > int(min_id)]
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
    async def send_message(self, peer, text, reply_to=None, schedule=None):
        if self.send_message_raises is not None:
            raise self.send_message_raises
        msg = FakeMessage(id=999, sender_id=1, text=text, out=True, peer_id=int(peer),
                          reply_to=reply_to)
        self.sent.append({"peer": int(peer), "text": text, "reply_to": reply_to,
                          "schedule": schedule})
        return msg

    async def send_file(self, peer, file, caption=None, voice_note=False,
                        video_note=False, force_document=False):
        if self.send_file_raises is not None:
            raise self.send_file_raises
        msg = FakeMessage(id=998, sender_id=1, text=caption, out=True, peer_id=int(peer))
        self.sent.append({
            "peer": int(peer), "file": str(file), "caption": caption,
            "voice_note": voice_note, "video_note": video_note,
            "force_document": force_document,
        })
        return msg

    async def forward_messages(self, to_peer, message_ids, from_peer):
        ids = message_ids if isinstance(message_ids, (list, tuple)) else [message_ids]
        self.forwarded.append(
            {"to_peer": int(to_peer), "message_ids": list(ids), "from_peer": int(from_peer)}
        )
        return [
            FakeMessage(id=900 + i, sender_id=1, text=f"fwd{mid}", out=True, peer_id=int(to_peer))
            for i, mid in enumerate(ids)
        ]

    async def edit_message(self, peer, message_id, text):
        self.edited.append({"peer": int(peer), "message_id": int(message_id), "text": text})
        return FakeMessage(id=int(message_id), sender_id=1, text=text, out=True, peer_id=int(peer))

    async def delete_messages(self, peer, message_ids, revoke=True):
        ids = message_ids if isinstance(message_ids, (list, tuple)) else [message_ids]
        self.deleted.append({"peer": int(peer), "message_ids": list(ids), "revoke": revoke})
        return None

    async def send_read_acknowledge(self, peer, max_id=None):
        self.read_acks.append({"peer": int(peer), "max_id": max_id})
        return True

    # --- moderation: restrict/ban via edit_permissions ---
    async def edit_permissions(self, entity, user=None, until_date=None, **rights):
        self.permissions.append(
            {"entity": int(entity), "user": int(user) if user is not None else None,
             "until_date": until_date, "rights": rights}
        )
        return None

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
def _isolated_environment(tmp_path, monkeypatch, request):
    """#229: isolate every test from the developer's real machine.

    Without this, any ``CliRunner().invoke(cli, ...)`` runs the real ``_load_dotenv()``,
    which reads the repo-root ``.env`` (real API creds, LangSmith keys) plus the real
    ``~/.tg/.env`` and writes them into ``os.environ`` via ``setdefault`` — OUTSIDE
    monkeypatch, so they survived into every later test (real Telegram connection
    attempts were observed in a full local run). Four layers:

    - cwd → tmp_path (no repo ``.env`` for ``_load_dotenv`` to find);
    - every ``TG_*`` / ``LANGSMITH_*`` / ``LANGCHAIN_*`` / ``SESSION_ENCRYPTION_KEY``
      var purged, then sandbox ``TG_HOME``/``TG_LOG_DIR`` set (tests that need a value
      ``setenv`` their own AFTER this fixture — unchanged);
    - ``DEFAULT_HOME``/``LEGACY_HOME`` module attributes → tmp dirs (they are read
      live but are NOT env-driven, and ``_load_dotenv`` always tries the fixed
      ``DEFAULT_HOME/.env`` fallback);
    - an ``os.environ`` snapshot diff-restored at teardown, closing the
      setdefault-past-monkeypatch leak class for anything a test loads itself.
    """
    if request.node.get_closest_marker("real_e2e") is not None:
        if os.environ.get("TG_RUN_REAL_E2E") != "1":
            pytest.skip("real Telegram E2E requires explicit TG_RUN_REAL_E2E=1")
        # Explicitly opted-in real E2E owns its real cwd/home/session namespace.
        # The module is skipped by default; see tests/test_e2e.py.
        yield
        return

    from tg_messenger.core import paths as core_paths
    from tg_messenger.core.paths import reset_tg_home_cache

    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("TG_", "LANGSMITH_", "LANGCHAIN_")) or key == "SESSION_ENCRYPTION_KEY":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TG_HOME", str(tmp_path / "tg-home"))
    # the CLI entrypoint calls setup_logging(); tests must never write ~/.tg_messenger/logs
    monkeypatch.setenv("TG_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(core_paths, "DEFAULT_HOME", tmp_path / "default-home")
    monkeypatch.setattr(core_paths, "LEGACY_HOME", tmp_path / "legacy-home")
    # tg_home() memoizes the resolved root per process; reset it around every test so
    # one test's TG_HOME / DEFAULT_HOME monkeypatch can't leak into the next via the cache
    reset_tg_home_cache()
    baseline = dict(os.environ)
    yield
    # diff-restore against the isolated baseline: _load_dotenv (and any direct
    # os.environ writes) bypass monkeypatch and would otherwise leak across tests
    for key in set(os.environ) - set(baseline):
        del os.environ[key]
    for key, value in baseline.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
    reset_tg_home_cache()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch, request):
    """#229: any test reaching a real outgoing network connection fails fast.

    Every network seam in the suite is faked (FakeTelethonClient, httpx.MockTransport,
    in-process TestClient) — an actual connect means a seam was missed, not a slow
    test. AF_UNIX stays allowed (local plumbing, e.g. event-loop internals).
    """
    if request.node.get_closest_marker("real_e2e") is not None:
        if os.environ.get("TG_RUN_REAL_E2E") != "1":
            pytest.skip("real Telegram E2E requires explicit TG_RUN_REAL_E2E=1")
        # Real E2E launches and probes an actual subprocess server. It is reachable
        # only through the explicit TG_RUN_REAL_E2E=1 gate in tests/test_e2e.py.
        return

    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address, *args, **kwargs)
        raise RuntimeError(
            f"test tried to open a real network connection to {address!r} — "
            "fake the client/transport seam instead (#229)"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def fake_client():
    return FakeTelethonClient()


@pytest.fixture
def session_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d
