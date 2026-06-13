"""Shared supported-language policy for translation features."""

from __future__ import annotations

SUPPORTED_LANG_CODES_ORDER = ("ru", "en", "es", "uk", "ja", "zh", "ko", "ar", "he", "el", "th")
SUPPORTED_LANG_CODES = frozenset(SUPPORTED_LANG_CODES_ORDER)
SUPPORTED_LANG_CODES_PROMPT = ", ".join(SUPPORTED_LANG_CODES_ORDER)


def clean_supported_lang_code(code: str | None) -> str | None:
    if code is None:
        return None
    lang = str(code).strip().lower()
    return lang if lang in SUPPORTED_LANG_CODES else None


def validate_supported_lang_code(code: str) -> str:
    lang = clean_supported_lang_code(code)
    if lang is None:
        raise ValueError("invalid language code")
    return lang
