"""OutboundSendCoordinator (#73) — one UI-agnostic outbound send service.

Web and TUI used to orchestrate the outbound translation flow in pieces: token/nonce
lifecycle (web only), the applies()->variants() fallback, the prepare timeout, send
execution, and source recording — each duplicated across the two UIs. This coordinator
centralizes all of it on top of the existing :class:`OutboundTranslator` (which keeps the
language resolver / applies / variants core). The UIs now call ``prepare`` then
``send_variant`` / ``send_original`` and render the result.

No UI and no LangChain imports — agent layer, stdlib + core + ``agent.outbound`` only.
The translator does the LLM work behind injected callables; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from tg_messenger.core.models import Message

logger = logging.getLogger(__name__)

PREPARE_TIMEOUT_SECONDS = 20.0
TOKEN_TTL_SECONDS = 5 * 60.0
TOKEN_MAX = 200

# A send function is whatever the UI passes (e.g. client.send_text bound to a peer/reply).
SendFn = Callable[[int, str], Awaitable[Message]]


class OutboundError(Exception):
    """A token could not be honoured (missing/expired/wrong owner/already sending)."""


@dataclass
class PrepareResult:
    """Outcome of :meth:`OutboundSendCoordinator.prepare`.

    ``status`` is one of: ``disabled`` (no translator wired / outbound off for the
    dialog), ``invalid_empty`` (blank draft), ``not_applicable`` (no translation needed),
    ``ready`` (``target_lang`` + ``variants`` + ``token`` populated), ``error`` (a logged
    timeout/exception — the UI should offer "send original").
    """

    status: str
    target_lang: str | None = None
    variants: list[str] = field(default_factory=list)
    token: str | None = None
    error: str | None = None


@dataclass
class _TokenEntry:
    dialog_id: int
    owner_id: str
    source_text: str
    variants: tuple[str, ...]
    expires_at: float
    sending: bool = False  # marked True before the network send (double-submit guard)


class OutboundSendCoordinator:
    """Outbound send orchestration shared by Web and TUI.

    Injected: ``outbound`` (an OutboundTranslator or None), ``store`` (MessageStore for
    ``record_outgoing``; carries ``.storage`` for the user-lang lookup), ``storage`` (KV;
    defaults to ``store.storage``), ``clock``/``timeout``/``token_ttl``/``token_max`` for
    deterministic tests. Reads (history) happen once per ``prepare`` (delegated to the
    translator's ``prepare_variants``).
    """

    def __init__(
        self,
        *,
        outbound,
        store,
        storage=None,
        env=None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = PREPARE_TIMEOUT_SECONDS,
        token_ttl: float = TOKEN_TTL_SECONDS,
        token_max: int = TOKEN_MAX,
    ):
        self._outbound = outbound
        self._store = store
        self._storage = storage if storage is not None else getattr(store, "storage", None)
        self._env = env
        self._clock = clock
        self._timeout = timeout
        self._token_ttl = token_ttl
        self._token_max = max(1, token_max)
        self._tokens: OrderedDict[str, _TokenEntry] = OrderedDict()
        self._lock = asyncio.Lock()  # guards the token store (mark/consume/restore)

    # --- prepare -------------------------------------------------------------

    async def prepare(
        self,
        dialog_id: int,
        draft_text: str,
        *,
        telegram_lang_code: str | None = None,
        owner_id: str = "",
    ) -> PrepareResult:
        if not draft_text.strip():
            return PrepareResult(status="invalid_empty")
        if self._outbound is None:
            return PrepareResult(status="disabled")
        try:
            target_lang, variants = await asyncio.wait_for(
                self._outbound.prepare_variants(
                    dialog_id, draft_text, telegram_lang_code=telegram_lang_code
                ),
                timeout=self._timeout,
            )
        except TimeoutError:
            logger.warning("outbound prepare timed out for dialog %s", dialog_id)
            return PrepareResult(status="error", error="Translation timed out.")
        except Exception:
            logger.warning(
                "outbound prepare failed for dialog %s", dialog_id, exc_info=True
            )
            return PrepareResult(status="error", error="Translation failed.")
        if target_lang is None:
            return PrepareResult(status="not_applicable")
        token = await self._remember_token(
            dialog_id=dialog_id, owner_id=owner_id, source_text=draft_text, variants=variants
        )
        return PrepareResult(
            status="ready", target_lang=target_lang, variants=list(variants), token=token
        )

    # --- send ----------------------------------------------------------------

    async def send_variant(
        self,
        dialog_id: int,
        token: str,
        variant_text: str,
        send_fn: SendFn,
        *,
        owner_id: str = "",
    ) -> Message:
        """Send a chosen variant: validate+mark the token, send, then record + consume.

        On a send failure the token is restored to a retryable state (until its TTL), so
        the user can retry without re-running prepare. A concurrent double-submit of the
        same token is rejected before the second network send (mark-sending under a lock).
        """
        entry = await self._mark_sending(
            token, dialog_id=dialog_id, owner_id=owner_id, variant_text=variant_text
        )
        try:
            msg = await send_fn(dialog_id, variant_text)
        except Exception:
            await self._restore_token(token)
            raise
        await self._consume_token(token)
        await self._record_source(dialog_id, msg, source_text=entry.source_text)
        return msg

    async def send_original(self, dialog_id: int, text: str, send_fn: SendFn) -> Message:
        """Send the untranslated original — no token, no source recording."""
        return await send_fn(dialog_id, text)

    # --- source recording ----------------------------------------------------

    async def _record_source(self, dialog_id: int, message: Message, *, source_text: str) -> None:
        if not source_text or self._store is None or self._storage is None:
            return
        from tg_messenger.agent.translate import get_user_lang

        try:
            source_lang = await get_user_lang(self._storage, env=self._env)
            if source_lang:
                await self._store.record_outgoing(
                    dialog_id, message, source_text=source_text, source_lang=source_lang
                )
                message.translated_text = source_text
        except Exception:
            logger.warning(
                "failed to record outbound source for dialog %s", dialog_id, exc_info=True
            )

    # --- token lifecycle -----------------------------------------------------

    def _token_count(self) -> int:
        return len(self._tokens)

    def _prune(self, now: float) -> None:
        expired = [t for t, e in self._tokens.items() if e.expires_at <= now and not e.sending]
        for t in expired:
            self._tokens.pop(t, None)
        while len(self._tokens) > self._token_max:
            # evict the oldest non-sending token; never drop one mid-send
            for t, e in list(self._tokens.items()):
                if not e.sending:
                    self._tokens.pop(t, None)
                    break
            else:
                break

    async def _remember_token(
        self, *, dialog_id: int, owner_id: str, source_text: str, variants
    ) -> str:
        async with self._lock:
            now = self._clock()
            self._prune(now)
            token = secrets.token_urlsafe(24)
            self._tokens[token] = _TokenEntry(
                dialog_id=dialog_id,
                owner_id=owner_id,
                source_text=source_text,
                variants=tuple(variants),
                expires_at=now + self._token_ttl,
            )
            self._tokens.move_to_end(token)
            self._prune(now)
            return token

    async def _mark_sending(
        self, token: str, *, dialog_id: int, owner_id: str, variant_text: str
    ) -> _TokenEntry:
        async with self._lock:
            now = self._clock()
            entry = self._tokens.get(token)
            if entry is None:
                logger.warning("rejecting missing/expired outbound token for dialog %s", dialog_id)
                raise OutboundError("outbound selection expired")
            if entry.expires_at <= now:
                self._tokens.pop(token, None)
                logger.warning("rejecting expired outbound token for dialog %s", dialog_id)
                raise OutboundError("outbound selection expired")
            if entry.sending:
                logger.warning("rejecting already-sending outbound token for dialog %s", dialog_id)
                raise OutboundError("outbound send already in progress")
            if entry.dialog_id != dialog_id:
                logger.warning("rejecting outbound token for wrong dialog %s", dialog_id)
                raise OutboundError("outbound token does not match this dialog")
            if entry.owner_id != owner_id:
                logger.warning("rejecting outbound token for wrong owner")
                raise OutboundError("outbound token does not match this client")
            if variant_text not in entry.variants:
                logger.warning("rejecting non-variant outbound send for dialog %s", dialog_id)
                raise OutboundError("text is not one of the offered variants")
            entry.sending = True  # claim it before the network send (double-submit guard)
            return entry

    async def _consume_token(self, token: str) -> None:
        async with self._lock:
            self._tokens.pop(token, None)

    async def _restore_token(self, token: str) -> None:
        async with self._lock:
            entry = self._tokens.get(token)
            if entry is not None:
                entry.sending = False  # retryable again until its TTL elapses
