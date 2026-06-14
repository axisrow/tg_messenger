"""Local dialog filtering — pure, network-free functions over the cached dialog list.

Telegram-style exact-ish lookup: title substring (case-insensitive), username
(with/without ``@``, exact or prefix), and id (exact, or the positive form of a
marked group/channel id). The input is an already-fetched ``list[Dialog]`` (the #8
read cache), so no request is ever made. Global content search across all chats is
deliberately absent — that's tg_content_factory's job (umbrella #6).
"""

from __future__ import annotations

from tg_messenger.core.models import Dialog


def _matches(dialog: Dialog, query: str) -> bool:
    q = query.casefold()
    # title substring, case-insensitive
    if q in dialog.title.casefold():
        return True
    # username: with or without leading @, exact or prefix
    if dialog.username:
        uname = dialog.username.casefold()
        needle = q[1:] if q.startswith("@") else q
        if needle and uname.startswith(needle):
            return True
    # id: exact marked id, or its positive form (users type the number without the sign)
    raw = query.strip()
    if raw.lstrip("-").isdigit():
        if raw == str(dialog.id) or raw == str(abs(dialog.id)):
            return True
    return False


def filter_dialogs(dialogs: list[Dialog], query: str) -> list[Dialog]:
    """Return dialogs matching ``query`` by title / username / id. Empty query → all."""
    q = query.strip()
    if not q:
        return dialogs
    return [d for d in dialogs if _matches(d, q)]


def can_send_in(dialogs: list[Dialog], dialog_id: int) -> bool:
    """Whether OUR account may POST in ``dialog_id``, resolved over an already-fetched
    dialog list (the #8 read cache) — pure, network-free. The ONE lookup rule shared by
    every UI (#90): an unknown dialog is fail-safe writable (True), matching
    ``_entity_can_send`` and the core ``SendForbiddenError`` net that catches a real
    rejection at send time.

    POST capability only. Reactions are a SEPARATE capability (#86) and never call this —
    a read-only chat can still react; a true rejection surfaces as SendForbiddenError.
    """
    return next((d.can_send for d in dialogs if d.id == dialog_id), True)
