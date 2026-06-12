"""Cached inbound translation helpers.

This module contains no LLM imports. The factory injects a plain async
``translate_fn`` so the core read paths stay testable without the agent extra.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence

from tg_messenger.core.message_store import (
    get_message_translation,
    set_message_translation,
    upsert_message_for_translation,
)
from tg_messenger.core.models import Message

logger = logging.getLogger(__name__)

TranslateFn = Callable[[Sequence[tuple[int, str]], str], Awaitable[Mapping[int, str | None]]]

USER_LANG_KEY = "user_lang"
DEFAULT_BATCH_SIZE = 20

_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def needs_translation(text: str | None, target_lang: str | None) -> bool:
    """Whether text should be sent to the translator.

    We only skip empty/media/emoji-only messages here. Same-script text still
    goes to the translator because script is not language detection: e.g. Spanish
    or German text should still be translated for an English target. The injected
    translator may return ``None`` when the message is already in the target
    language, and that result is cached.
    """
    if not text or not target_lang:
        return False
    letters = [ch for ch in text if _LETTER_RE.match(ch)]
    if not letters:
        return False
    return True


async def get_user_lang(storage, env=None) -> str | None:
    value = await storage.get_value(USER_LANG_KEY)
    if value:
        return str(value)
    source = os.environ if env is None else env
    value = source.get("TG_USER_LANG")
    return str(value).strip() or None if value is not None else None


async def set_user_lang(storage, code: str | None) -> None:
    if code is None:
        await storage.execute("DELETE FROM kv WHERE key = ?", (USER_LANG_KEY,))
        return
    await storage.set_value(USER_LANG_KEY, code.strip().lower())


def translate_model_from_env(env=None) -> str | None:
    source = os.environ if env is None else env
    return (source.get("TG_TRANSLATE_MODEL") or source.get("TG_AGENT_MODEL") or "").strip() or None


class Translator:
    def __init__(
        self,
        *,
        storage,
        translate_fn: TranslateFn,
        env=None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self._storage = storage
        self._translate_fn = translate_fn
        self._env = os.environ if env is None else env
        self._batch_size = int(batch_size)

    async def target_lang(self) -> str | None:
        return await get_user_lang(self._storage, self._env)

    async def set_target_lang(self, code: str | None) -> None:
        await set_user_lang(self._storage, code)

    async def translate_history(self, dialog_id: int, messages: Sequence[Message]) -> list[Message]:
        target = await self.target_lang()
        if not target:
            return list(messages)
        result: list[Message] = []
        pending: list[tuple[Message, str]] = []
        for message in messages:
            if message.out:
                result.append(message)
                continue
            translated, needs_llm = await self._prepare_message(message, target)
            result.append(translated)
            if needs_llm and message.text:
                pending.append((message, message.text))
        if pending:
            by_id = await self._translate_batches(pending, target)
            result = [
                message.model_copy(update={"translated_text": by_id[message.id]})
                if message.id in by_id and by_id[message.id] is not None
                else message
                for message in result
            ]
        return result

    async def translate_message(self, message: Message) -> Message:
        translated = await self.translate_history(message.dialog_id, [message])
        return translated[0] if translated else message

    async def _prepare_message(self, message: Message, target: str) -> tuple[Message, bool]:
        try:
            cached = await get_message_translation(self._storage, message.dialog_id, message.id, target)
            if cached is not None:
                return message.model_copy(update={"translated_text": cached["text"]}), False
            if not needs_translation(message.text, target):
                await upsert_message_for_translation(self._storage, message)
                await set_message_translation(
                    self._storage, message.dialog_id, message.id, lang=target, text=None
                )
                return message.model_copy(update={"translated_text": None}), False
            return message, True
        except Exception:
            logger.exception("translation cache lookup failed for message %s", message.id)
            return message, False

    async def _translate_batches(
        self,
        pending: Sequence[tuple[Message, str]],
        target: str,
    ) -> dict[int, str | None]:
        updates: dict[int, str | None] = {}
        for i in range(0, len(pending), self._batch_size):
            chunk = pending[i:i + self._batch_size]
            payload = [(message.id, text) for message, text in chunk]
            try:
                translated = await self._translate_fn(payload, target)
            except Exception:
                logger.exception("translation batch failed")
                continue
            for message, _ in chunk:
                text = translated.get(message.id)
                try:
                    await upsert_message_for_translation(self._storage, message)
                    await set_message_translation(
                        self._storage, message.dialog_id, message.id, lang=target, text=text
                    )
                except Exception:
                    logger.exception("failed to cache translation for message %s", message.id)
                    continue
                updates[message.id] = text
        return updates
