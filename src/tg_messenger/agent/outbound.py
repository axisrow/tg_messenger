"""Outbound translation picker service.

No LLM imports here: detection and variants are injected by ``agent.factory``.
This write-path service never blocks a send; callers degrade to normal sends
when ``applies`` returns None or when variant generation fails.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import BaseModel

from tg_messenger.agent.suggest import ContextMessage, StyleProfile, load_style_profile
from tg_messenger.agent.translate import get_user_lang
from tg_messenger.core.languages import clean_supported_lang_code, validate_supported_lang_code

logger = logging.getLogger(__name__)

DIALOG_LANG_KEY = "dialog_lang_{dialog_id}"
OUTBOUND_ENABLED_KEY = "outbound_enabled_{dialog_id}"
AUTO_DIALOG_LANG_TTL_SECONDS = 86400.0
NON_LATIN_SCRIPT_LANGS = {"ar", "el", "he", "ja", "ko", "ru", "th", "zh"}
CYRILLIC_SCRIPT_LANGS = {"be", "bg", "kk", "ky", "mk", "mn", "ru", "sr", "tg", "uk", "uz"}
HAN_SCRIPT_LANGS = {"ja", "ko", "lzh", "yue", "zh"}


class DialogLang(BaseModel):
    lang: str
    source: Literal["auto", "manual"] = "auto"
    detected_at: float | None = None


OutboundVariantsFn = Callable[[str, str, StyleProfile | None, Sequence[ContextMessage]], Awaitable[list[str]]]
DetectLangFn = Callable[[Sequence[str]], Awaitable[str | None]]


def _dialog_lang_key(dialog_id: int) -> str:
    return DIALOG_LANG_KEY.format(dialog_id=int(dialog_id))


def _outbound_enabled_key(dialog_id: int) -> str:
    return OUTBOUND_ENABLED_KEY.format(dialog_id=int(dialog_id))


async def get_dialog_lang(storage, dialog_id: int) -> DialogLang | None:
    value = await storage.get_value(_dialog_lang_key(dialog_id))
    if value is None:
        return None
    return DialogLang.model_validate(value)


async def set_dialog_lang(
    storage,
    dialog_id: int,
    code: str | None,
    *,
    source: Literal["auto", "manual"] = "manual",
    detected_at: float | None = None,
) -> None:
    if code is None:
        await storage.execute("DELETE FROM kv WHERE key = ?", (_dialog_lang_key(dialog_id),))
        return
    lang = validate_supported_lang_code(code)
    await storage.set_value(
        _dialog_lang_key(dialog_id),
        DialogLang(
            lang=lang,
            source=source,
            detected_at=detected_at if source == "auto" else None,
        ).model_dump(exclude_none=True),
    )


async def is_outbound_enabled(storage, dialog_id: int) -> bool:
    value = await storage.get_value(_outbound_enabled_key(dialog_id))
    return value is not False


async def set_outbound_enabled(storage, dialog_id: int, enabled: bool) -> None:
    key = _outbound_enabled_key(dialog_id)
    if enabled:
        await storage.execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        await storage.set_value(key, False)


def detect_script_lang(texts: Sequence[str]) -> str | None:
    lang = _dominant_script_lang(texts)
    return _script_lang_to_supported(lang)


def _script_lang_to_supported(lang: str | None) -> str | None:
    # Latin and Cyrillic are shared by many languages. Pure Han falls back to
    # zh for callers without an injected exact detector.
    if lang == "han":
        return "zh"
    if lang in {None, "latin", "ru"}:
        return None
    return clean_supported_lang_code(lang)


def _dominant_script_lang(texts: Sequence[str]) -> str | None:
    counts: dict[str, int] = {}
    for text in texts:
        for ch in text:
            lang = _char_lang(ch)
            if lang is not None:
                counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    if counts.get("ja") and counts.get("han"):
        counts["ja"] += counts.pop("han")
    lang, count = max(counts.items(), key=lambda item: item[1])
    total = sum(counts.values())
    if count / total < 0.70:
        return None
    return lang


def _clean_lang_code(code: str | None) -> str | None:
    return clean_supported_lang_code(code)


def _char_lang(ch: str) -> str | None:
    cp = ord(ch)
    if 0xAC00 <= cp <= 0xD7AF:
        return "ko"
    if 0x3040 <= cp <= 0x30FF:
        return "ja"
    if 0x4E00 <= cp <= 0x9FFF:
        return "han"
    if 0x0E00 <= cp <= 0x0E7F:
        return "th"
    if 0x0590 <= cp <= 0x05FF:
        return "he"
    if 0x0370 <= cp <= 0x03FF:
        return "el"
    if 0x0600 <= cp <= 0x06FF:
        return "ar"
    if 0x0400 <= cp <= 0x052F:
        return "ru"
    if ("A" <= ch <= "Z") or ("a" <= ch <= "z") or (0x00C0 <= cp <= 0x024F):
        return "latin"
    return None


class OutboundTranslator:
    def __init__(
        self,
        *,
        store,
        storage,
        variants_fn: OutboundVariantsFn,
        detect_lang_fn: DetectLangFn | None = None,
        history_limit: int = 30,
        env=None,
        clock=time.time,
    ):
        self._store = store
        self._storage = storage
        self._variants_fn = variants_fn
        self._detect_lang_fn = detect_lang_fn
        self._history_limit = int(history_limit)
        self._env = env
        self._clock = clock

    @property
    def storage(self):
        return self._storage

    def _fresh_stored_dialog_lang(self, stored: DialogLang) -> str | None:
        if stored.source == "manual":
            return stored.lang
        if stored.detected_at is not None and self._clock() - stored.detected_at < AUTO_DIALOG_LANG_TTL_SECONDS:
            return stored.lang
        return None

    async def dialog_lang(
        self,
        dialog_id: int,
        *,
        telegram_lang_code: str | None = None,
        history: Sequence | None = None,
    ) -> str | None:
        stored = await get_dialog_lang(self._storage, dialog_id)
        if stored is not None:
            fresh = self._fresh_stored_dialog_lang(stored)
            if fresh is not None:
                return fresh
        telegram_lang = clean_supported_lang_code(telegram_lang_code)
        if telegram_lang is not None:
            await set_dialog_lang(
                self._storage,
                dialog_id,
                telegram_lang,
                source="auto",
                detected_at=self._clock(),
            )
            return telegram_lang
        if telegram_lang_code is not None and telegram_lang_code.strip():
            logger.warning("unsupported telegram language code %r", telegram_lang_code)
        history = list(history) if history is not None else await self._store.history(dialog_id, self._history_limit)
        incoming = [(m.text or "").strip() for m in history if not m.out and (m.text or "").strip()]
        if not incoming:
            return None
        script_lang = _dominant_script_lang(incoming)
        lang = _script_lang_to_supported(script_lang)
        if script_lang == "han" and self._detect_lang_fn is not None:
            try:
                lang = await self._detect_lang_fn(incoming[:10])
            except Exception:
                logger.exception("dialog language detection failed for %s", dialog_id)
                return None
        if lang is None and self._detect_lang_fn is not None:
            try:
                lang = await self._detect_lang_fn(incoming[:10])
            except Exception:
                logger.exception("dialog language detection failed for %s", dialog_id)
                return None
        if lang is None:
            return None
        raw_lang = lang
        lang = clean_supported_lang_code(raw_lang)
        if lang is None:
            logger.warning("dialog language detection returned invalid code %r", raw_lang)
            return None
        await set_dialog_lang(self._storage, dialog_id, lang, source="auto", detected_at=self._clock())
        return lang

    async def applies(
        self,
        dialog_id: int,
        draft_text: str,
        *,
        telegram_lang_code: str | None = None,
        history: Sequence | None = None,
    ) -> str | None:
        try:
            if not await is_outbound_enabled(self._storage, dialog_id):
                return None
            user_lang = _clean_lang_code(await get_user_lang(self._storage, self._env))
            if not user_lang:
                return None
            dialog_lang = await self.dialog_lang(
                dialog_id,
                telegram_lang_code=telegram_lang_code,
                history=history,
            )
            if dialog_lang is None or dialog_lang == user_lang:
                return None
            script_lang = _dominant_script_lang([draft_text])
            if script_lang is None:
                return None
            if script_lang == "ru":
                if user_lang not in CYRILLIC_SCRIPT_LANGS:
                    return None
                draft_lang = await self._detect_draft_lang(draft_text)
                if draft_lang == dialog_lang:
                    return None
                if draft_lang is not None and draft_lang != user_lang:
                    return None
                return dialog_lang
            if script_lang == "han":
                if user_lang not in HAN_SCRIPT_LANGS:
                    return None
                draft_lang = await self._detect_draft_lang(draft_text)
                if draft_lang == dialog_lang:
                    return None
                if draft_lang is not None and draft_lang != user_lang:
                    return None
                return dialog_lang
            if script_lang != "latin":
                if script_lang != user_lang:
                    return None
                if script_lang == dialog_lang:
                    return None
                return dialog_lang
            if user_lang in NON_LATIN_SCRIPT_LANGS:
                return None
            draft_lang = await self._detect_draft_lang(draft_text)
            if draft_lang == dialog_lang:
                return None
            if draft_lang is not None and draft_lang != user_lang:
                return None
            return dialog_lang
        except Exception:
            logger.exception("outbound translation applicability failed for dialog %s", dialog_id)
            return None

    async def _detect_draft_lang(self, draft_text: str) -> str | None:
        if self._detect_lang_fn is None:
            return None
        try:
            lang = await self._detect_lang_fn([draft_text])
        except Exception:
            logger.exception("draft language detection failed")
            return None
        cleaned = _clean_lang_code(lang)
        if lang is not None and cleaned is None:
            logger.warning("draft language detection returned invalid code %r", lang)
        return cleaned

    async def variants(self, dialog_id: int, draft_text: str, target_lang: str) -> list[str]:
        history = await self._store.history(dialog_id, self._history_limit)
        return await self._variants_from_history(dialog_id, draft_text, target_lang, history)

    async def prepare_variants(
        self,
        dialog_id: int,
        draft_text: str,
        *,
        telegram_lang_code: str | None = None,
    ) -> tuple[str | None, list[str]]:
        history = await self._store.history(dialog_id, self._history_limit)
        target_lang = await self.applies(
            dialog_id,
            draft_text,
            telegram_lang_code=telegram_lang_code,
            history=history,
        )
        if target_lang is None:
            return None, []
        variants = await self._variants_from_history(dialog_id, draft_text, target_lang, history)
        return target_lang, variants

    async def _variants_from_history(
        self,
        dialog_id: int,
        draft_text: str,
        target_lang: str,
        history: Sequence,
    ) -> list[str]:
        context = [ContextMessage(out=bool(m.out), text=m.text or "") for m in history]
        profile = await load_style_profile(self._storage, dialog_id)
        variants = await self._variants_fn(draft_text, target_lang, profile, context)
        result = [v.strip() for v in variants if v and v.strip()][:3]
        if not result:
            raise ValueError("outbound translator returned no variants")
        return result
