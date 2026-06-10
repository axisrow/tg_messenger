"""Agent runner: the listen → filter → orchestrate → reply loop.

No LLM imports here — the orchestrator is any object with
``async handle(dialog_id, text) -> str``. One message failing must never
kill the loop: errors are logged (never swallowed) and, optionally, a short
notice is sent back to the dialog.
"""

from __future__ import annotations

import logging

from tg_messenger.agent.config import AgentConfig
from tg_messenger.core.models import IncomingEvent

logger = logging.getLogger(__name__)

ERROR_NOTICE = "Sorry, something went wrong while processing your message."


class AgentRunner:
    def __init__(self, client, orchestrator, *, config: AgentConfig, notify_errors: bool = False):
        self._client = client
        self._orchestrator = orchestrator
        self._config = config
        self._notify_errors = notify_errors

    async def _resolve_allowed_ids(self) -> frozenset[int]:
        """Allowlist ids + usernames resolved against the dialog list (once, at start).

        Invariant: filtering checks ``message.sender_id``; for DMs Telegram uses the
        peer's user id as the dialog id, so resolving a username to ``dialog.id``
        yields exactly the sender_id of that user's future messages.
        """
        allowed = set(self._config.allow_ids)
        if self._config.allow_usernames:
            dialogs = await self._client.dialogs(dm_only=True)
            by_username = {d.username.lower(): d.id for d in dialogs if d.username}
            for uname in sorted(self._config.allow_usernames):
                if uname in by_username:
                    allowed.add(by_username[uname])
                else:
                    logger.warning(
                        "allowlist entry @%s not found among dialogs — ignored"
                        " (use the numeric id for contacts you have no dialog with)", uname,
                    )
        return frozenset(allowed)

    async def run(self) -> None:
        allowed = None if self._config.allow_all else await self._resolve_allowed_ids()
        async for event in self._client.listen():
            await self._handle_event(event, allowed)

    async def _handle_event(self, event: IncomingEvent, allowed: frozenset[int] | None) -> None:
        message = event.message
        if message.out:
            # defence in depth: core subscribes with NewMessage(incoming=True),
            # so our own replies never reach listen() in the first place
            return
        if not message.text:
            logger.debug("skip message %s in dialog %s: no text", message.id, event.dialog_id)
            return
        if allowed is not None and message.sender_id not in allowed:
            logger.debug("skip message from %s: not in allowlist", message.sender_id)
            return
        try:
            # client.typing never raises by contract (core logs its own failures)
            async with self._client.typing(event.dialog_id):
                reply = await self._orchestrator.handle(event.dialog_id, message.text)
                await self._client.send_text(event.dialog_id, reply)
        except Exception:
            logger.exception("agent failed on message %s in dialog %s", message.id, event.dialog_id)
            if self._notify_errors:
                try:
                    await self._client.send_text(event.dialog_id, ERROR_NOTICE)
                except Exception:
                    logger.exception("failed to send error notice to dialog %s", event.dialog_id)
