"""Цикл 13: AgentRunner — listen → фильтры → orchestrator.handle → send_text.

Оркестратор — любой объект с ``handle(dialog_id, text) -> str`` (duck typing),
клиент — стаб с программируемым listen(). Сети и LLM нет.
"""

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from tg_messenger.agent.config import AgentConfig
from tg_messenger.agent.media import MAX_IMAGE_BYTES, ImageInput
from tg_messenger.agent.runner import AgentRunner
from tg_messenger.core.models import Dialog, IncomingEvent, MediaRef, Message


def ev(dialog_id=7, sender_id=7, text="ping", out=False, msg_id=1, media=None):
    return IncomingEvent(
        dialog_id=dialog_id,
        message=Message(id=msg_id, dialog_id=dialog_id, sender_id=sender_id, out=out,
                        text=text, media=media, date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
    )


def photo(size=1024, mime_type="image/png"):
    return MediaRef(kind="photo", size=size, mime_type=mime_type, downloadable=True)


def voice():
    return MediaRef(kind="voice", mime_type="audio/ogg", downloadable=True)


def cfg(**kw):
    defaults = dict(model="anthropic:claude-x", allow_all=True)
    defaults.update(kw)
    return AgentConfig(**defaults)


class StubClient:
    def __init__(self, events, dialogs=()):
        self._events = list(events)
        self._dialogs = list(dialogs)
        self.sent = []
        self.dialogs_calls = 0
        self.typing_active = []
        self.downloads = []
        self.download_payload = b"img-bytes"
        self.download_fail = False

    async def download_message_media(self, peer, message_id, dest):
        self.downloads.append((peer, message_id))
        if self.download_fail:
            raise RuntimeError("net down")
        path = Path(dest) / "p.jpg"
        path.write_bytes(self.download_payload)
        return str(path)

    def typing(self, peer):
        stub = self

        class _Typing:
            async def __aenter__(self):
                stub.typing_active.append(peer)
                return self

            async def __aexit__(self, *exc):
                stub.typing_active.remove(peer)
                return False

        return _Typing()

    async def listen(self):
        for e in self._events:
            yield e

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        return self._dialogs

    async def send_text(self, peer, text):
        self.sent.append((peer, text))
        return Message(id=900, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))


class StubOrchestrator:
    def __init__(self):
        self.handled = []
        self.images = []  # image (или None) каждого вызова handle, по порядку
        self.fail_on = set()  # тексты, на которых handle взрывается

    async def handle(self, dialog_id, text, *, image=None):
        self.handled.append((dialog_id, text))
        self.images.append(image)
        if text in self.fail_on:
            raise RuntimeError("llm exploded")
        return f"re: {text}"


def make(events, *, config=None, dialogs=(), notify_errors=False):
    client = StubClient(events, dialogs)
    orch = StubOrchestrator()
    runner = AgentRunner(client, orch, config=config or cfg(), notify_errors=notify_errors)
    return runner, client, orch


async def test_incoming_message_is_handled_and_replied_to_same_dialog():
    runner, client, orch = make([ev(dialog_id=7, text="привет")])
    await runner.run()
    assert orch.handled == [(7, "привет")]
    assert client.sent == [(7, "re: привет")]


async def test_outgoing_messages_are_skipped():
    # core регистрирует NewMessage(incoming=True), свои ответы в listen не попадают —
    # этот фильтр в runner — защита в глубину
    runner, client, orch = make([ev(out=True), ev(text="ok", msg_id=2)])
    await runner.run()
    assert orch.handled == [(7, "ok")]


async def test_messages_without_text_are_skipped(caplog):
    runner, client, orch = make([ev(text=None), ev(text="ok", msg_id=2)])
    with caplog.at_level(logging.DEBUG, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(7, "ok")]
    assert any("no text" in r.message for r in caplog.records)


async def test_allowlist_by_id_filters_strangers(caplog):
    config = cfg(allow_all=False, allow_ids=frozenset({123}))
    events = [ev(dialog_id=99, sender_id=99, text="чужой"),
              ev(dialog_id=123, sender_id=123, text="свой", msg_id=2)]
    runner, client, orch = make(events, config=config)
    with caplog.at_level(logging.DEBUG, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(123, "свой")]
    assert client.sent == [(123, "re: свой")]
    assert any("allowlist" in r.message for r in caplog.records)


async def test_allow_all_processes_everyone():
    events = [ev(dialog_id=1, sender_id=1, text="a"), ev(dialog_id=2, sender_id=2, text="b", msg_id=2)]
    runner, client, orch = make(events)
    await runner.run()
    assert orch.handled == [(1, "a"), (2, "b")]


async def test_usernames_resolved_via_dialogs_once(caplog):
    config = cfg(allow_all=False, allow_usernames=frozenset({"ann", "ghost"}))
    dialogs = [Dialog(id=7, title="Ann", username="Ann")]
    events = [ev(dialog_id=7, sender_id=7, text="от Энн"),
              ev(dialog_id=8, sender_id=8, text="мимо", msg_id=2)]
    runner, client, orch = make(events, config=config, dialogs=dialogs)
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(7, "от Энн")]
    assert client.dialogs_calls == 1  # резолв один раз на старте
    assert any("ghost" in r.message for r in caplog.records)  # нерезолвленный — warning


async def test_error_in_one_message_does_not_kill_the_loop(caplog):
    runner, client, orch = make([ev(text="boom"), ev(text="ok", msg_id=2)])
    orch.fail_on = {"boom"}
    with caplog.at_level(logging.ERROR, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(7, "boom"), (7, "ok")]
    assert client.sent == [(7, "re: ok")]  # упавшее сообщение без ответа
    assert any(r.exc_info for r in caplog.records)  # не молча: logger.exception


async def test_notify_errors_sends_short_notice():
    runner, client, orch = make([ev(text="boom")], notify_errors=True)
    orch.fail_on = {"boom"}
    await runner.run()
    assert len(client.sent) == 1
    peer, text = client.sent[0]
    assert peer == 7 and "re:" not in text  # короткая заглушка, не обычный ответ


async def test_failing_error_notice_is_logged_and_loop_survives(caplog):
    runner, client, orch = make([ev(text="boom"), ev(text="ok", msg_id=2)], notify_errors=True)
    orch.fail_on = {"boom"}

    original_send = client.send_text

    async def flaky_send(peer, text):
        if "re:" not in text:
            raise RuntimeError("telegram down")
        return await original_send(peer, text)

    client.send_text = flaky_send
    with caplog.at_level(logging.ERROR, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(7, "boom"), (7, "ok")]
    assert sum("error notice" in r.message for r in caplog.records) == 1


async def test_typing_indicator_active_while_handling():
    runner, client, orch = make([ev(text="привет")])
    seen = {}
    orig_handle = orch.handle

    async def spying_handle(dialog_id, text):
        seen["typing_during_handle"] = list(client.typing_active)
        return await orig_handle(dialog_id, text)

    orch.handle = spying_handle
    await runner.run()
    assert seen["typing_during_handle"] == [7]  # индикатор горит, пока агент думает
    assert client.typing_active == []  # и гаснет после ответа
    # сбои самого индикатора не дело runner'а: client.typing() по контракту
    # не бросает (см. _SafeChatAction в core и тесты в test_client.py)


async def test_allowlist_checks_sender_id_not_dialog_id():
    # инвариант: фильтр идёт по sender_id; для DM dialog.id == sender_id,
    # но тест фиксирует выбор поля расходящимися значениями
    config = cfg(allow_all=False, allow_ids=frozenset({123}))
    events = [ev(dialog_id=500, sender_id=123, text="разрешён"),
              ev(dialog_id=123, sender_id=500, text="чужой", msg_id=2)]
    runner, client, orch = make(events, config=config)
    await runner.run()
    assert orch.handled == [(500, "разрешён")]


# --- Цикл 20: диспетчеризация медиа (voice / photo / прочее) ---


async def test_voice_message_is_logged_and_skipped(caplog):
    runner, client, orch = make([ev(text=None, media=voice()), ev(text="ok", msg_id=2)])
    with caplog.at_level(logging.INFO, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(7, "ok")]  # голосовое не дошло до оркестратора, цикл жив
    assert any("voice" in r.message for r in caplog.records)


async def test_photo_with_caption_goes_to_handle_with_image():
    runner, client, orch = make([ev(text="что это?", media=photo())])
    await runner.run()
    assert orch.handled == [(7, "что это?")]
    (image,) = orch.images
    assert isinstance(image, ImageInput)
    assert base64.b64decode(image.base64_data) == client.download_payload
    assert image.mime_type == "image/png"
    assert client.sent == [(7, "re: что это?")]


async def test_photo_without_caption_passes_empty_text():
    runner, client, orch = make([ev(text=None, media=photo())])
    await runner.run()
    assert orch.handled == [(7, "")]
    assert orch.images[0] is not None


async def test_text_message_passes_no_image():
    runner, client, orch = make([ev(text="привет")])
    await runner.run()
    assert orch.images == [None]
    assert client.downloads == []


async def test_photo_from_stranger_is_not_downloaded():
    # allowlist проверяется ДО скачивания — чужие не тратят сеть/диск
    config = cfg(allow_all=False, allow_ids=frozenset({123}))
    runner, client, orch = make([ev(sender_id=99, text=None, media=photo())], config=config)
    await runner.run()
    assert client.downloads == []
    assert orch.handled == []


async def test_photo_download_error_is_logged_and_loop_survives(caplog):
    runner, client, orch = make([ev(text=None, media=photo()), ev(text="ok", msg_id=2)])
    client.download_fail = True
    with caplog.at_level(logging.ERROR, logger="tg_messenger.agent.runner"):
        await runner.run()
    assert orch.handled == [(7, "ok")]
    assert any(r.exc_info for r in caplog.records)  # не молча: logger.exception


async def test_oversize_photo_is_skipped_without_reply():
    runner, client, orch = make([ev(text=None, media=photo(size=MAX_IMAGE_BYTES + 1))])
    await runner.run()
    assert client.downloads == []  # warning логирует agent.media (см. test_agent_media)
    assert orch.handled == []
    assert client.sent == []


async def test_document_with_caption_goes_to_text_path():
    doc = MediaRef(kind="document", file_name="report.pdf", downloadable=True)
    runner, client, orch = make([ev(text="глянь файл", media=doc)])
    await runner.run()
    assert orch.handled == [(7, "глянь файл")]
    assert orch.images == [None]
    assert client.downloads == []
