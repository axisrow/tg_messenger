"""Cached inbound translation helpers.

This module contains no LLM imports. The factory injects a plain async
``translate_fn`` so the core read paths stay testable without the agent extra.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Literal

from tg_messenger.core.languages import (
    clean_supported_lang_code,
    parse_lang_codes,
    validate_supported_lang_code,
)
from tg_messenger.core.message_store import (
    clear_all_translations,
    get_message_translation,
    set_message_translation,
    upsert_message_for_translation,
)
from tg_messenger.core.models import Message

logger = logging.getLogger(__name__)

# The injected LLM translator receives, per batch: the (id, text) pairs, the target language,
# the source languages to SKIP (return null), and — when set — the ONLY source languages to
# translate (null for everything else). skip_langs/only_langs default to empty for callers that
# don't care, so the old "translate everything into target" behaviour is preserved.
TranslateFn = Callable[
    [Sequence[tuple[int, str]], str, Sequence[str], Sequence[str]],
    Awaitable[Mapping[int, str | None]],
]

USER_LANG_KEY = "user_lang"
TRANSLATE_MODE_KEY = "translate_mode"
KNOWN_LANGS_KEY = "known_langs"
UNKNOWN_LANGS_KEY = "unknown_langs"
DEFAULT_BATCH_SIZE = 20

# Inbound translation modes (what to translate, by SOURCE language):
#   off          — translation disabled (equivalent to no user_lang)
#   all_unknown  — translate everything except the user's known languages (default)
#   skip_known   — translate everything except the explicit known_langs list
#   only_unknown — translate ONLY the explicit unknown_langs list
TranslateMode = Literal["off", "all_unknown", "skip_known", "only_unknown"]
TRANSLATE_MODES: tuple[TranslateMode, ...] = ("off", "all_unknown", "skip_known", "only_unknown")
DEFAULT_TRANSLATE_MODE: TranslateMode = "all_unknown"

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
        lang = clean_supported_lang_code(str(value))
        if lang is None:
            logger.warning("unsupported stored user language code")
        return lang
    source = os.environ if env is None else env
    value = source.get("TG_USER_LANG")
    if value is None:
        return None
    lang = clean_supported_lang_code(str(value))
    if lang is None and str(value).strip():
        logger.warning("unsupported TG_USER_LANG value")
    return lang


async def set_user_lang(storage, code: str | None) -> None:
    if code is None:
        await storage.execute("DELETE FROM kv WHERE key = ?", (USER_LANG_KEY,))
    else:
        lang = validate_supported_lang_code(code)
        await storage.set_value(USER_LANG_KEY, lang)
    # the target language is part of the cache key; changing it must drop cached
    # "no translation needed" / stale-target rows so the next read re-decides.
    await clear_all_translations(storage)


async def get_translate_mode(storage, env=None) -> TranslateMode:
    """Resolve the inbound translation mode.

    A stored mode wins; otherwise we infer from legacy state so existing setups keep working:
    a configured target language (kv or ``TG_USER_LANG``) means ``all_unknown``, none means ``off``.
    """
    value = await storage.get_value(TRANSLATE_MODE_KEY)
    if value in TRANSLATE_MODES:
        return value  # type: ignore[return-value]
    if value is not None:
        logger.warning("unknown stored translate_mode %r; falling back", value)
    target = await get_user_lang(storage, env)
    return DEFAULT_TRANSLATE_MODE if target else "off"


async def set_translate_mode(storage, mode: str | None) -> None:
    if mode is None:
        await storage.execute("DELETE FROM kv WHERE key = ?", (TRANSLATE_MODE_KEY,))
    elif mode in TRANSLATE_MODES:
        await storage.set_value(TRANSLATE_MODE_KEY, mode)
    else:
        raise ValueError(f"invalid translate mode: {mode}")
    await clear_all_translations(storage)


async def _get_lang_list(storage, key: str) -> list[str]:
    raw = await storage.get_value(key)
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        logger.warning("corrupt language list in %s; ignoring", key)
        return []
    if not isinstance(value, list):
        logger.warning("non-list language list in %s; ignoring", key)
        return []
    # re-validate on read so an out-of-policy code (e.g. a dropped supported lang) is filtered out
    out: list[str] = []
    for item in value:
        lang = clean_supported_lang_code(str(item))
        if lang and lang not in out:
            out.append(lang)
    return out


async def _set_lang_list(storage, key: str, codes) -> None:
    if isinstance(codes, str):
        codes = parse_lang_codes(codes)
    cleaned: list[str] = []
    for code in codes or []:
        cleaned.append(validate_supported_lang_code(str(code)))
    deduped = list(dict.fromkeys(cleaned))
    await storage.set_value(key, json.dumps(deduped))
    await clear_all_translations(storage)


async def get_known_langs(storage) -> list[str]:
    return await _get_lang_list(storage, KNOWN_LANGS_KEY)


async def set_known_langs(storage, codes) -> None:
    await _set_lang_list(storage, KNOWN_LANGS_KEY, codes)


async def get_unknown_langs(storage) -> list[str]:
    return await _get_lang_list(storage, UNKNOWN_LANGS_KEY)


async def set_unknown_langs(storage, codes) -> None:
    await _set_lang_list(storage, UNKNOWN_LANGS_KEY, codes)


async def resolve_skip_only(storage, target: str | None, env=None) -> tuple[list[str], list[str]]:
    """Resolve (skip_langs, only_langs) for the current mode.

    ``skip_langs`` — source languages NOT to translate (return null). ``only_langs`` — when
    non-empty, translate ONLY these source languages (null for everything else). At most one is
    non-empty. The target language is always implicitly skipped (no point translating a message
    already in the language we'd translate INTO) under ``all_unknown`` and under ``only_unknown``
    with an EMPTY whitelist — an empty whitelist means "translate everything that differs from the
    target" rather than "translate nothing".
    """
    mode = await get_translate_mode(storage, env)
    if mode == "skip_known":
        return await get_known_langs(storage), []
    if mode == "only_unknown":
        unknown = await get_unknown_langs(storage)
        if unknown:
            return [], unknown
        # empty whitelist = translate everything that differs from the target:
        # no "only" restriction, just skip the target language itself.
        return ([target] if target else []), []
    # all_unknown: skip the user's known languages plus the target language itself
    known = await get_known_langs(storage)
    skip = list(known)
    if target and target not in skip:
        skip.append(target)
    return skip, []


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

    async def get_settings(self) -> dict:
        """Snapshot the inbound-translation settings for a UI (mode + target + lists)."""
        return {
            "mode": await get_translate_mode(self._storage, self._env),
            "target": await get_user_lang(self._storage, self._env),
            "known": await get_known_langs(self._storage),
            "unknown": await get_unknown_langs(self._storage),
        }

    async def set_settings(
        self,
        *,
        mode: str,
        target: str | None = None,
        known=None,
        unknown=None,
    ) -> None:
        """Persist inbound-translation settings from a UI.

        Validates everything (codes via the core policy) BEFORE writing the mode, so a bad list
        raises without half-applying. Each setter clears the translation cache (policy changed).
        Only the lists relevant to the chosen mode need be supplied; passing the others is harmless.
        """
        if mode not in TRANSLATE_MODES:
            raise ValueError(f"invalid translate mode: {mode}")
        if target is not None:
            await set_user_lang(self._storage, target or None)
        if known is not None:
            await set_known_langs(self._storage, known)
        if unknown is not None:
            await set_unknown_langs(self._storage, unknown)
        await set_translate_mode(self._storage, mode)

    async def translate_history(self, dialog_id: int, messages: Sequence[Message]) -> list[Message]:
        target = await self.target_lang()
        mode = await get_translate_mode(self._storage, self._env)
        if not target or mode == "off":
            return list(messages)
        skip_langs, only_langs = await resolve_skip_only(self._storage, target, self._env)
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
            by_id = await self._translate_batches(pending, target, skip_langs, only_langs)
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
            cached = await get_message_translation(
                self._storage,
                message.dialog_id,
                message.id,
                target,
                source_text=message.text,
            )
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
        skip_langs: Sequence[str] = (),
        only_langs: Sequence[str] = (),
    ) -> dict[int, str | None]:
        updates: dict[int, str | None] = {}
        for i in range(0, len(pending), self._batch_size):
            chunk = pending[i:i + self._batch_size]
            payload = [(message.id, text) for message, text in chunk]
            try:
                translated = await self._translate_fn(payload, target, skip_langs, only_langs)
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
