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

Report durability (#228): a Telegram send is irreversible, so its completion/
failure report must never be lost. Failed reports go to a durable on-disk
queue (``reports_path``; in-memory when None) and are flushed at the start of
every ``process_once()`` — before any new claim — until the factory acks them.
The factory treats an identical replay of a terminal report as success and a
conflicting one as 409 (dropped: the other outcome already won).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from tg_messenger.interop.factory_client import FactoryError

logger = logging.getLogger(__name__)

DEFAULT_TYPES = ["dm_reply", "chat_answer"]
IDLE_SLEEP = 5.0  # seconds between empty polls

_SleepFn = Callable[[float], Awaitable[None]]


class ReportQueue:
    """Durable list of unacknowledged reports; JSON-file-backed when given a path.

    Item shape: ``{"kind": "complete", "task_id": ..., "result_payload": ...}`` or
    ``{"kind": "fail", "task_id": ..., "error": ...}`` — exactly what the factory
    methods take, so a flush replays the original report byte-for-byte.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.items: list[dict] = []
        if path is None:
            return
        try:
            self.items = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # don't let one corrupt file wedge the worker; keep it for forensics
            logger.exception("report queue %s is unreadable — starting empty", path)
            corrupt = path.with_suffix(path.suffix + ".corrupt")
            try:
                path.replace(corrupt)
            except OSError:
                logger.exception("could not move corrupt report queue aside")

    def add(self, item: dict) -> None:
        self.items.append(item)
        self._save()

    def ack(self) -> None:
        self.items.pop(0)
        self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False), "utf-8")
        tmp.replace(self._path)


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
        reports_path: Path | str | None = None,
    ) -> None:
        self._client = client
        self._factory = factory
        self._types = list(types) if types else list(DEFAULT_TYPES)
        self._agent = agent
        self._sleep = sleep or _default_sleep
        self._idle_sleep = idle_sleep
        self._reports = ReportQueue(Path(reports_path) if reports_path is not None else None)

    async def run(self) -> None:
        """Forever: claim → execute → report; idle-sleep when the queue is empty."""
        while True:
            try:
                handled = await self.process_once()
            except FactoryError as exc:
                # a dead poll (factory unreachable, ...) must not kill the loop;
                # pending reports survive in the queue and retry next round.
                # A 4xx (bad password, bad request) will never succeed — exit.
                if exc.status_code is not None and exc.status_code < 500:
                    raise
                logger.exception("worker: poll step failed")
                handled = False
            except Exception:
                logger.exception("worker: poll step failed")
                handled = False
            if not handled:
                await self._sleep(self._idle_sleep)

    async def process_once(self) -> bool:
        """One step. Returns True if a task was claimed (success OR handled failure)."""
        await self._flush_reports()
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

    async def _flush_reports(self) -> None:
        """Replay queued reports oldest-first until acked; stop on the first failure.

        A 409 means a conflicting report already won on the factory side — the
        replay can never succeed, so it is dropped (with a loud log) instead of
        blocking the queue forever.
        """
        while self._reports.items:
            item = self._reports.items[0]
            try:
                if item["kind"] == "complete":
                    await self._factory.complete_task(item["task_id"], item["result_payload"])
                else:
                    await self._factory.fail_task(item["task_id"], item["error"])
            except FactoryError as exc:
                if exc.status_code == 409:
                    logger.error(
                        "worker: report for task %s conflicts with the factory state"
                        " (another outcome won) — dropping",
                        item["task_id"],
                    )
                    self._reports.ack()
                    continue
                break
            except Exception:
                logger.exception("worker: report replay for task %s failed", item["task_id"])
                break
            else:
                self._reports.ack()

    async def _safe_complete_task(self, task_id: str, result: dict) -> None:
        try:
            await self._factory.complete_task(task_id, result)
        except Exception:
            logger.exception("worker: failed to report task %s completion", task_id)
            self._reports.add({"kind": "complete", "task_id": task_id, "result_payload": result})

    async def _safe_fail_task(self, task_id: str, error: str) -> None:
        try:
            await self._factory.fail_task(task_id, error)
        except Exception:
            logger.exception("worker: failed to report task %s failure", task_id)
            self._reports.add({"kind": "fail", "task_id": task_id, "error": error})

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
