"""Pure text-formatting helpers for the TUI bubbles and dialog rows.

No Textual/Telethon imports — these operate on already-mapped core models and plain
strings, so they stay unit-testable standalone (the project's parsing/format split, like
``core.search``). Re-exported from ``tg_messenger.tui.app`` for backward-compatible imports.
"""

from __future__ import annotations

import unicodedata

from tg_messenger.core.models import format_author

# #129: the three zero-width glyphs whose DISPLAY WIDTH Rich/Textual and the terminal disagree
# on — the text/emoji variation selectors (FE0E/FE0F) and the ZWJ that fuses emoji sequences.
# Stripping ONLY these realigns the bubble border without touching any load-bearing letter.
_WIDTH_AMBIGUOUS_ZERO_WIDTH = "\ufe0e\ufe0f\u200d"


def _terminal_safe_display_text(value: str) -> str:
    """Return a terminal-safe display copy of Telegram-sourced text.

    #126/#127: Rich/Textual and some terminals disagree on the display width of a few zero-width
    glyphs (variation selectors FE0E/FE0F, the emoji ZWJ), which knocked the bubble border out of
    alignment. We drop ONLY those three; the bubble's right CSS padding absorbs any residual drift.

    #129: we deliberately do NOT drop combining marks (Unicode ``Mn``) or replace emoji — those
    are load-bearing. Thai/Devanagari/Arabic/Hebrew vowels & tone marks are letters; removing them
    changes the message. Emoji stay visible so the displayed text matches Telegram.
    """
    normalized = unicodedata.normalize("NFC", value)
    if not any(ch in _WIDTH_AMBIGUOUS_ZERO_WIDTH for ch in normalized):
        return normalized
    return "".join(ch for ch in normalized if ch not in _WIDTH_AMBIGUOUS_ZERO_WIDTH)


def _message_body(message) -> str:
    """The bubble body for a message: the ``[id] text`` line (or ``<media>``)."""
    return f"[{message.id}] {message.text or '<media>'}"


def _message_author_line(message, *, show_author: bool) -> str | None:
    """The group author line (#108) when the caller asks for it, else ``None``.

    #118: author is now passed to MessageBubble as a SEPARATE field rather than glued onto the
    body and re-parsed — so untrusted message text (a body that happens to contain ``\\n[``)
    can never be misread as an author line. Content (``format_author``) is unchanged.
    """
    return format_author(message) if show_author else None


def _split_id_prefix(body: str) -> tuple[str, str]:
    """Split a body into its ``[id] `` prefix and the rest, for dimming the id (#113).

    Returns ``("[id] ", rest)`` when the body opens with a ``[...] `` head, else ``("", body)``
    so non-prefixed bodies (e.g. media echoes) pass through unstyled.
    """
    if body.startswith("["):
        close = body.find("] ")
        if close != -1:
            return body[: close + 2], body[close + 2 :]
    return "", body
