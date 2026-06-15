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


def parse_lang_codes(value: str | None) -> list[str]:
    """Parse a comma/space-separated list of language codes into a validated, deduped list.

    Order-stable, dedupes, drops blanks; an unsupported code raises ValueError naming it
    (so a UI can surface exactly which token was bad). An empty/blank input is an empty list.
    """
    if not value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(value).replace(",", " ").split():
        lang = clean_supported_lang_code(raw)
        if lang is None:
            raise ValueError(f"invalid language code: {raw}")
        if lang not in seen:
            seen.add(lang)
            out.append(lang)
    return out
