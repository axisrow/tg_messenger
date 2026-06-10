"""Цикл 19: agent/media.py — скачивание фото для vision (stdlib+core, без LLM-стека).

Сетевой шов: стаб-клиент с download_message_media, пишущий байты в dest.
"""

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tg_messenger.agent import media as media_mod
from tg_messenger.agent.media import MAX_IMAGE_BYTES, ImageInput, download_image
from tg_messenger.core.models import MediaRef, Message

PAYLOAD = b"\x89PNG fake image bytes"


def make_message(*, size=1024, mime_type="image/png", msg_id=42):
    return Message(
        id=msg_id, dialog_id=7, sender_id=7, out=False,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        text=None,
        media=MediaRef(kind="photo", size=size, mime_type=mime_type, downloadable=True),
    )


class StubDownloadClient:
    def __init__(self, payload=PAYLOAD, *, fail=False, no_media=False):
        self.payload = payload
        self.fail = fail
        self.no_media = no_media
        self.calls = []

    async def download_message_media(self, peer, message_id, dest):
        self.calls.append({"peer": peer, "message_id": message_id, "dest": str(dest)})
        if self.fail:
            raise RuntimeError("boom")
        if self.no_media:
            return None
        path = Path(dest) / "photo.png"
        path.write_bytes(self.payload)
        return str(path)


async def test_download_image_happy_path():
    client = StubDownloadClient()
    image = await download_image(client, 7, make_message())
    assert isinstance(image, ImageInput)
    assert base64.b64decode(image.base64_data) == PAYLOAD
    assert image.mime_type == "image/png"
    (call,) = client.calls
    assert call["peer"] == 7
    assert call["message_id"] == 42


async def test_download_image_mime_falls_back_to_jpeg():
    client = StubDownloadClient()
    image = await download_image(client, 7, make_message(mime_type=None))
    assert image.mime_type == "image/jpeg"


async def test_oversize_by_declared_size_skips_download(caplog):
    client = StubDownloadClient()
    msg = make_message(size=MAX_IMAGE_BYTES + 1)
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.media"):
        image = await download_image(client, 7, msg)
    assert image is None
    assert client.calls == []  # лимит проверяется ДО скачивания
    assert any("too large" in r.message for r in caplog.records)  # не молча


async def test_oversize_by_actual_bytes_is_rejected(monkeypatch, caplog):
    # заявленный size может врать (или отсутствовать) — реальные байты тоже проверяются
    client = StubDownloadClient()
    monkeypatch.setattr(media_mod, "MAX_IMAGE_BYTES", len(PAYLOAD) - 1)
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.media"):
        image = await download_image(client, 7, make_message(size=None))
    assert image is None
    assert any("too large" in r.message for r in caplog.records)


async def test_no_media_returns_none():
    client = StubDownloadClient(no_media=True)
    image = await download_image(client, 7, make_message())
    assert image is None


async def test_tmp_dir_is_cleaned_up():
    client = StubDownloadClient()
    await download_image(client, 7, make_message())
    (call,) = client.calls
    assert not Path(call["dest"]).exists()


async def test_tmp_dir_is_cleaned_up_on_error():
    client = StubDownloadClient(fail=True)
    with pytest.raises(RuntimeError):  # ошибки скачивания обрабатывает вызывающий (runner)
        await download_image(client, 7, make_message())
    (call,) = client.calls
    assert not Path(call["dest"]).exists()
