"""Image download for the vision pipeline (stdlib + core only, no LLM imports).

Download errors propagate — the runner's per-message try/except logs them and
keeps the loop alive. Everything skipped here is logged (no silent failures).
"""

from __future__ import annotations

import base64
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tg_messenger.core.models import Message

logger = logging.getLogger(__name__)

IMAGE_PLACEHOLDER = "[изображение]"
MAX_IMAGE_BYTES = 10_000_000


@dataclass(frozen=True)
class ImageInput:
    base64_data: str
    mime_type: str


async def download_image(client, dialog_id: int, message: Message) -> ImageInput | None:
    """Download the photo of ``message`` into memory as base64.

    Returns ``None`` (with a warning) when the image is over ``MAX_IMAGE_BYTES``
    or the message turns out to carry no media. Only server-compressed Telegram
    photos are handled; an image sent as a document never reaches this code.
    """
    declared = message.media.size if message.media else None
    if declared is not None and declared > MAX_IMAGE_BYTES:
        logger.warning(
            "image in message %s is too large (%s bytes > %s) — skipping",
            message.id, declared, MAX_IMAGE_BYTES,
        )
        return None
    with tempfile.TemporaryDirectory(prefix="tg_messenger_img_") as tmpdir:
        path = await client.download_message_media(dialog_id, message.id, tmpdir)
        if path is None:
            logger.warning("message %s carries no downloadable media — skipping", message.id)
            return None
        data = Path(path).read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        # the declared size can lie (or be absent) — re-check the actual bytes
        logger.warning(
            "image in message %s is too large (%s bytes > %s) — skipping",
            message.id, len(data), MAX_IMAGE_BYTES,
        )
        return None
    mime = (message.media.mime_type if message.media else None) or "image/jpeg"
    return ImageInput(base64_data=base64.b64encode(data).decode("ascii"), mime_type=mime)
