"""DeletionWatcher — backs up own deleted messages to Saved Messages.

Telegram's MessageDeleted update carries only message ids (plus the chat for
channels/supergroups) and never the actor, so a bounded cache of recently sent
own messages is the only way to (a) recognise OUR messages among foreign
deletions and (b) recover the lost text. Best-effort by nature: Telegram does
not always deliver the update, and the cache lives in memory.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from tg_messenger.core.client import is_channel_or_megagroup_id
from tg_messenger.core.models import MessagesDeletedEvent, OutgoingEvent

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 1000
MEDIA_PLACEHOLDER = "<медиа/без текста>"
NOTIFY_HEADER = "🗑 Удалено в «{title}» (id {dialog_id}):"


@dataclass(frozen=True)
class CachedMessage:
    dialog_id: int
    message_id: int
    text: str | None
    date: datetime


class DeletionWatcher:
    """listen_outgoing → кэш; listen_deleted → матч по кэшу → бэкап в Saved Messages."""

    def __init__(
        self,
        client,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
        echo: Callable[[str], None] | None = None,
    ):
        self._client = client
        self._cache_size = cache_size
        self._echo = echo or (lambda line: None)
        self._cache: OrderedDict[tuple[int, int], CachedMessage] = OrderedDict()
        # bounded like _cache — a long-running watcher must not grow without limit
        self._titles: OrderedDict[int, str] = OrderedDict()
        self._self_id: int = 0

    async def run(self) -> None:
        me = await self._client.get_me()
        self._self_id = me.id
        # gather, не TaskGroup: TaskGroup оборачивает KeyboardInterrupt
        # в BaseExceptionGroup и ломает Ctrl+C-обработку в CLI
        await asyncio.gather(self._consume_outgoing(), self._consume_deleted())

    async def _consume_outgoing(self) -> None:
        async for ev in self._client.listen_outgoing():
            self._remember(ev)

    async def _consume_deleted(self) -> None:
        async for ev in self._client.listen_deleted():
            await self._handle_deleted(ev)

    def _remember(self, ev: OutgoingEvent) -> None:
        if ev.dialog_id == self._self_id:
            return  # Saved Messages: свои уведомления не кэшируются — нет цикла
        message = ev.message
        self._cache[(ev.dialog_id, message.id)] = CachedMessage(
            dialog_id=ev.dialog_id, message_id=message.id, text=message.text, date=message.date
        )
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _match(self, ev: MessagesDeletedEvent) -> list[CachedMessage]:
        """Найти и ИЗЪЯТЬ свои записи (повторное событие не дублирует уведомление)."""
        if ev.chat_id is not None:
            hits = [self._cache.pop((ev.chat_id, mid), None) for mid in ev.message_ids]
            return [h for h in hits if h is not None]
        # Событие БЕЗ chat_id Telegram шлёт только для ЛС/малых групп, где message id
        # глобальны — канальные записи кэша с их пер-канальными id обязаны быть
        # отсечены, иначе ложный матч по совпадению id.
        ids = set(ev.message_ids)
        keys = [
            key
            for key, entry in self._cache.items()
            if entry.message_id in ids and not is_channel_or_megagroup_id(entry.dialog_id)
        ]
        return [self._cache.pop(key) for key in keys]

    async def _handle_deleted(self, ev: MessagesDeletedEvent) -> None:
        matched = self._match(ev)
        if not matched:
            logger.debug("deleted ids %s: no cached own messages — ignoring", ev.message_ids)
            return
        by_dialog: dict[int, list[CachedMessage]] = {}
        for entry in matched:
            by_dialog.setdefault(entry.dialog_id, []).append(entry)
        for dialog_id, entries in by_dialog.items():
            title = await self._title(dialog_id)
            lines = [NOTIFY_HEADER.format(title=title, dialog_id=dialog_id)]
            for entry in sorted(entries, key=lambda e: e.message_id):
                lines.append(f"[{entry.date:%Y-%m-%d %H:%M}] {entry.text or MEDIA_PLACEHOLDER}")
            try:
                await self._client.send_text(self._self_id, "\n".join(lines))
            except Exception:
                logger.exception("failed to send deletion notice for dialog %s", dialog_id)
                continue
            self._echo(f"🗑 {title}: {len(entries)} удалённое(ых) сообщение(й) сохранено в Saved Messages")

    async def _title(self, dialog_id: int) -> str:
        if dialog_id not in self._titles:
            try:
                self._titles[dialog_id] = await self._client.entity_title(dialog_id)
            except Exception:
                logger.warning("could not resolve title for dialog %s — using the id", dialog_id)
                self._titles[dialog_id] = str(dialog_id)
            while len(self._titles) > self._cache_size:
                self._titles.popitem(last=False)
        return self._titles[dialog_id]
