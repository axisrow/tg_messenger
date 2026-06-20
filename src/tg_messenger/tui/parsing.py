"""Pure composer-command parsers for the TUI.

``shlex``-based, no filesystem / no Textual — the path-existence check stays in the handler
(BEFORE any worker/network), so these are unit-testable standalone. Re-exported from
``tg_messenger.tui.app`` for backward-compatible imports.
"""

from __future__ import annotations

import shlex


def parse_media_command(text: str) -> tuple[str, str | None] | None:
    """Parse a composer ``@PATH [caption]`` media command.

    Pure (no filesystem). Returns ``(path, caption)`` or ``None`` when ``text`` is
    not a media command (doesn't start with ``@`` or has no path after it).
    The path may be quoted (``@"with spaces.png" cap``); the rest is the caption.
    """
    if not text.startswith("@"):
        return None
    rest = text[1:]
    if not rest.strip():
        return None
    try:
        # split off only the first token (the path), keep the remainder verbatim
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    path = tokens[0]
    if not path:
        return None
    # caption = everything after the first token, re-derived from the raw text so
    # internal quoting/spacing of the caption is preserved literally
    remainder = _strip_first_token(rest)
    caption = remainder.strip() or None
    return path, caption


def parse_lang_command(text: str) -> tuple[str, str | None] | None:
    parts = text.split(maxsplit=1)
    if not parts or parts[0] != "/lang":
        return None
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError("usage: /lang CODE|auto|on|off")
    value = parts[1].strip().lower()
    if value in {"auto", "on", "off"}:
        return value, None
    return "set", value


def parse_tlang_command(text: str) -> tuple[str, str | None] | None:
    """`/tlang CODE|off` — set/clear the INBOUND reading language (#126).

    Distinct from /lang (which drives OUTBOUND translation). Returns ("set", code) or
    ("off", None), or None when the text isn't a /tlang command; raises on a missing argument.
    """
    parts = text.split(maxsplit=1)
    if not parts or parts[0] != "/tlang":
        return None
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError("usage: /tlang CODE|off")
    value = parts[1].strip().lower()
    if value == "off":
        return "off", None
    return "set", value


def _strip_first_token(s: str) -> str:
    """Return ``s`` with its first shlex token (quoted or not) removed."""
    lexer = shlex.shlex(s, posix=True)
    lexer.whitespace_split = True
    try:
        lexer.get_token()  # consume the first token
    except ValueError:
        return ""
    # lexer.instream holds the unconsumed tail
    return lexer.instream.read()
