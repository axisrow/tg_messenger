"""Worker: poll the factory for tasks, execute them via the core client, report back.

Same shape as ``agent.runner.AgentRunner``: a poll loop where one failing task
must never kill the worker — the error is logged (``logger.exception``, never
swallowed) and reported to the factory via ``fail_task``; the loop keeps going.

Executors map a task ``type`` to core-client work:
- ``dm_reply`` / ``chat_answer``: payload ``{peer, text}`` -> ``send_text`` ->
  ``{sent: msg_id}``. With ``{peer, prompt}`` instead of ``text`` it needs the
  optional agent (injected) — without it the task fails with a clear message.
- ``fetch_history``: ``{peer, limit?, offset_id?}`` -> ``history`` ->
  ``{messages: [...]}`` (Pydantic models dumped to dicts).
- ``fetch_dialogs``: ``{dm_only?}`` -> ``dialogs`` -> ``{dialogs: [...]}``.

``process_once()`` runs a single claim→execute→report step (no loop) so tests
drive it directly; ``run()`` is the production loop with an idle ``sleep``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_TYPES = ["dm_reply", "chat_answer"]
IDLE_SLEEP = 5.0  # seconds between empty polls

_SleepFn = Callable[[float], Awaitable[None]]


async def _default_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class Worker:
    """Polls ``factory`` for tasks of ``types`` and executes them over ``client``.

    ``agent`` (optional) is any object with ``async handle(dialog_id, text) -> str``
    — needed only for prompt-based reply tasks (the ``[agent]`` extra).
    """

    def __init__(
        self,
        client,
        factory,
        *,
        types: list[str] | None = None,
        agent=None,
        sleep: _SleepFn | None = None,
        idle_sleep: float = IDLE_SLEEP,
    ) -> None:
        self._client = client
        self._factory = factory
        self._types = list(types) if types else list(DEFAULT_TYPES)
        self._agent = agent
        self._sleep = sleep or _default_sleep
        self._idle_sleep = idle_sleep

    async def run(self) -> None:
        """Forever: claim → execute → report; idle-sleep when the queue is empty."""
        while True:
            handled = await self.process_once()
            if not handled:
                await self._sleep(self._idle_sleep)

    async def process_once(self) -> bool:
        """One step. Returns True if a task was claimed (success OR handled failure)."""
        task = await self._factory.claim_next(self._types)
        if task is None:
            return False
        task_id = task.get("id")
        try:
            result = await self._execute(task)
        except Exception as exc:
            logger.exception("worker: task %s (%s) failed", task_id, task.get("type"))
            await self._safe_fail_task(task_id, f"{type(exc).__name__}: {exc}")
            return True
        await self._safe_complete_task(task_id, result)
        return True

    async def _safe_complete_task(self, task_id: str, result: dict) -> None:
        try:
            await self._factory.complete_task(task_id, result)
        except Exception:
            logger.exception("worker: failed to report task %s completion", task_id)

    async def _safe_fail_task(self, task_id: str, error: str) -> None:
        try:
            await self._factory.fail_task(task_id, error)
        except Exception:
            logger.exception("worker: failed to report task %s failure", task_id)

    async def _execute(self, task: dict) -> dict:
        task_type = task.get("type")
        payload = task.get("payload") or {}
        if task_type in ("dm_reply", "chat_answer"):
            return await self._reply(payload)
        if task_type == "fetch_history":
            return await self._fetch_history(payload)
        if task_type == "fetch_dialogs":
            return await self._fetch_dialogs(payload)
        raise ValueError(f"unknown task type {task_type!r}")

    async def _reply(self, payload: dict) -> dict:
        peer = payload["peer"]
        text = payload.get("text")
        if text is None:
            prompt = payload.get("prompt")
            if prompt is None:
                raise ValueError("reply task needs either 'text' or 'prompt'")
            if self._agent is None:
                raise RuntimeError(
                    "prompt-based reply requires the agent — install the [agent] extra"
                    " and run the worker with an agent configured"
                )
            text = await self._agent.handle(peer, prompt)
        message = await self._client.send_text(peer, text)
        return {"sent": getattr(message, "id", None)}

    async def _fetch_history(self, payload: dict) -> dict:
        peer = payload["peer"]
        limit = payload.get("limit", 50)
        offset_id = payload.get("offset_id", 0)
        messages = await self._client.history(peer, limit=limit, offset_id=offset_id)
        return {"messages": [m.model_dump(mode="json") for m in messages]}

    async def _fetch_dialogs(self, payload: dict) -> dict:
        dm_only = payload.get("dm_only", True)
        dialogs = await self._client.dialogs(dm_only=dm_only)
        return {"dialogs": [d.model_dump(mode="json") for d in dialogs]}
