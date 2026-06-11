"""Telegram tools for the deep agent — plain async functions over the core client.

No LangChain imports: deepagents builds tool schemas straight from the
signature, and the docstring is the prompt the model sees. Every call goes
through ``StandaloneTelegramClient`` methods, so flood-wait retry is already
applied.

The optional factory tools (``factory_search``/``factory_create_task``) talk to
tg_content_factory. They import ``interop`` (httpx) LAZILY, inside the function
body — ``agent/tools.py`` itself stays httpx-free, and the tools only appear in
the toolset when a ``factory_url`` is configured (TG_FACTORY_URL).
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tg_messenger.core.models import message_line


def _make_factory_client(base_url: str, password: str, **kwargs):
    """Build a FactoryClient (httpx imported here, lazily — keeps tools.py httpx-free).

    A seam tests patch to avoid real httpx.
    """
    from tg_messenger.interop.factory_client import FactoryClient

    return FactoryClient(base_url=base_url, password=password, **kwargs)


def make_telegram_tools(
    client, *, factory_url: str | None = None, factory_password: str | None = None
) -> list[Callable]:
    """Build the Telegram toolset closed over a connected core client.

    When ``factory_url`` is set, the tg_content_factory tools (search + task
    creation) are appended; otherwise they're omitted entirely.
    """

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

    tools: list[Callable] = [send_telegram_message, read_telegram_history, list_telegram_dialogs]

    if factory_url:
        async def factory_search(query: str, identifier: str, limit: int = 20) -> str:
            """Search tg_content_factory's indexed message archive (the agent's long memory).

            Use this to recall what was said in a chat the factory has indexed —
            beyond the recent history Telegram itself returns.

            Args:
                query: Free-text search query.
                identifier: Chat the factory indexes by (@username or numeric id).
                limit: Max number of messages to return.
            """
            async with _make_factory_client(factory_url, factory_password or "") as factory:
                hits = await factory.search_messages(identifier, query, limit=limit)
            if not hits:
                return f"No factory matches for {query!r} in {identifier}."
            return json.dumps(hits, ensure_ascii=False)

        async def factory_create_task(type: str, payload: dict) -> str:
            """Enqueue a background task on tg_content_factory; returns the task id.

            Args:
                type: Task type the factory understands (e.g. 'dm_reply').
                payload: Task parameters (JSON object).
            """
            async with _make_factory_client(factory_url, factory_password or "") as factory:
                task_id = await factory.create_task(type, payload)
            return f"Created factory task {task_id} (type={type})."

        tools += [factory_search, factory_create_task]

    return tools
