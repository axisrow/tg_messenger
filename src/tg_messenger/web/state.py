"""Per-browser-client sent-tracking helpers for the web SSE echo suppression.

Each open browser gets a bounded ``OrderedDict`` bucket of message/reaction keys it itself
sent, so the SSE stream skips the optimistic echo the POST already rendered (#58). Pure data
structures — no FastAPI/network. Re-exported from ``tg_messenger.web.app`` so the routes (closures
inside ``build_app``) keep resolving these names as module globals.
"""

from __future__ import annotations

from collections import OrderedDict

from tg_messenger.core.cache import bounded_remember


def _sent_bucket(sent_ids_by_client: OrderedDict, client_id: str) -> OrderedDict:
    """Return the bounded sent-message set for one browser client."""
    client_id = _bounded_client_id(client_id)
    bucket = sent_ids_by_client.get(client_id)
    if bucket is None:
        bucket = OrderedDict()
        sent_ids_by_client[client_id] = bucket
    sent_ids_by_client.move_to_end(client_id)
    while len(sent_ids_by_client) > 100:
        sent_ids_by_client.popitem(last=False)
    return bucket


def _bounded_client_id(client_id: str) -> str:
    return client_id[:80]


def _remember_sent(sent_ids: OrderedDict, dialog_id: int, message_id: int) -> None:
    """Record a sent message key so its outgoing echo isn't re-streamed."""
    bounded_remember(sent_ids, (dialog_id, message_id))


def _remember_sent_reaction(
    sent_reactions: OrderedDict,
    dialog_id: int,
    message_id: int,
    emoticon: str | None,
) -> None:
    """Record a sent reaction key so its live echo isn't re-streamed."""
    bounded_remember(sent_reactions, (dialog_id, message_id, emoticon))
