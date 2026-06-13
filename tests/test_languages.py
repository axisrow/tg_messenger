"""Direct tests for the shared supported-language helpers (#73)."""

from __future__ import annotations

import pytest

from tg_messenger.core.languages import (
    SUPPORTED_LANG_CODES,
    clean_supported_lang_code,
    validate_supported_lang_code,
)


def test_clean_none_and_empty_and_whitespace():
    assert clean_supported_lang_code(None) is None
    assert clean_supported_lang_code("") is None
    assert clean_supported_lang_code("   ") is None


def test_clean_normalizes_case_and_whitespace():
    assert clean_supported_lang_code("EN") == "en"
    assert clean_supported_lang_code("  Ru ") == "ru"


def test_clean_accepts_every_supported_code():
    for code in SUPPORTED_LANG_CODES:
        assert clean_supported_lang_code(code) == code
        assert clean_supported_lang_code(code.upper()) == code


def test_clean_rejects_unsupported():
    assert clean_supported_lang_code("fr") is None
    assert clean_supported_lang_code("xx") is None


def test_validate_returns_clean_code():
    assert validate_supported_lang_code("EN") == "en"
    for code in SUPPORTED_LANG_CODES:
        assert validate_supported_lang_code(code) == code


def test_validate_rejects_unsupported_without_leaking_value():
    with pytest.raises(ValueError, match="invalid language code") as exc:
        validate_supported_lang_code("fr")
    # the message must not echo the bad code back
    assert "fr" not in str(exc.value)


def test_validate_rejects_empty_and_whitespace():
    with pytest.raises(ValueError, match="invalid language code"):
        validate_supported_lang_code("")
    with pytest.raises(ValueError, match="invalid language code"):
        validate_supported_lang_code("   ")
