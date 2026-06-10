"""Telegram tools for the deep agent — plain async functions over the core client.

No LangChain imports: deepagents builds tool schemas straight from the
signature, and the docstring is the prompt the model sees. Every call goes
through ``StandaloneTelegramClient`` methods, so flood-wait retry is already
applied.
"""

from __future__ import annotations

from collections.abc import Callable

from tg_messenger.core.models import message_line


def make_telegram_tools(client) -> list[Callable]:
    """Build the Telegram toolset closed over a connected core client."""

    async def send_telegram_message(peer_id: int, text: str) -> str:
        """Send a text message to a Telegram dialog.

        Args:
            peer_id: Numeric dialog id (get it from list_telegram_dialogs).
            text: Message text to send.
        """
        message = await client.send_text(peer_id, text)
        return f"Sent message id={message.id} to dialog {peer_id}."

    async def read_telegram_history(peer_id: int, limit: int = 20) -> str:
        """Read recent messages of a Telegram dialog, oldest first.

        Args:
            peer_id: Numeric dialog id (get it from list_telegram_dialogs).
            limit: Max number of messages to return.
        """
        messages = await client.history(peer_id, limit=limit)
        if not messages:
            return f"No messages in dialog {peer_id}."
        return "\n".join(message_line(m) for m in messages)

    async def list_telegram_dialogs() -> str:
        """List the user's direct-message dialogs (id, title, @username, unread count)."""
        dialogs = await client.dialogs(dm_only=True)
        if not dialogs:
            return "No dialogs found."
        lines = []
        for d in dialogs:
            uname = f" @{d.username}" if d.username else ""
            unread = f" ({d.unread} unread)" if d.unread else ""
            lines.append(f"{d.id}\t{d.title}{uname}{unread}")
        return "\n".join(lines)

    return [send_telegram_message, read_telegram_history, list_telegram_dialogs]
