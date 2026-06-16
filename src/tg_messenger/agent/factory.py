"""Production wiring of the agent — the ONLY module touching the LLM stack.

Everything heavy (init_chat_model, create_deep_agent) is imported and called
here, so version drift in langchain/deepagents stays contained in one file,
and tests stub these module-level names directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from tg_messenger.agent.config import AgentConfig, IntentSpec
from tg_messenger.agent.orchestrator import Orchestrator
from tg_messenger.agent.outbound import OutboundTranslator
from tg_messenger.agent.search import build_search_fn
from tg_messenger.agent.suggest import ContextMessage, StyleProfile, Suggester
from tg_messenger.agent.tools import make_telegram_tools
from tg_messenger.agent.translate import (
    Translator,
    get_cached_method,
    set_cached_method,
)
from tg_messenger.core.languages import SUPPORTED_LANG_CODES_PROMPT, clean_supported_lang_code

logger = logging.getLogger(__name__)
MODEL_CALL_TIMEOUT_SECONDS = 30.0
# Translation can run over a whole chat (up to N messages) with a slow reasoning model — give it
# a generous, env-overridable ceiling instead of the 30s used for short chat/vision/classify calls.
TRANSLATE_TIMEOUT_SECONDS = float(os.environ.get("TG_TRANSLATE_TIMEOUT", "600"))
# A tiny, predictable probe request used to detect whether a model honours native json_schema
# structured output (some OpenAI-compatible gateways silently ignore it and return prose).
_PROBE_TIMEOUT_SECONDS = 30.0


async def _ainvoke_with_timeout(model, messages):
    async with asyncio.timeout(MODEL_CALL_TIMEOUT_SECONDS):
        return await model.ainvoke(messages)


def build_classify_prompt(intents: Sequence[IntentSpec] = ()) -> str:
    """Промпт роутера из встроенных интентов + кастомных (имя — критерий из конфига)."""
    lines = [
        "You are an intent router for a Telegram assistant. Classify the user's"
        " message and answer with EXACTLY one word:",
        "- 'task' — the user asks to perform an action (send a message, look"
        " something up on the web, read chats, do multi-step work);",
    ]
    for spec in intents:
        lines.append(f"- '{spec.name}' — {spec.description};")
    lines.append(
        "- 'chat' — casual conversation, questions, small talk"
        " (use it when nothing else fits)."
    )
    names = ["task", *(spec.name for spec in intents), "chat"]
    lines.append("Answer with one word: " + " or ".join(names) + ".")
    return "\n".join(lines)


CHAT_SYSTEM_PROMPT = (
    "You are a friendly Telegram assistant. Reply briefly and naturally,"
    " in the language of the user's message."
)

TASK_SYSTEM_PROMPT = (
    "You are a Telegram assistant that completes the user's task using the"
    " available tools: Telegram actions (send messages, read history, list"
    " dialogs) and web search. Plan, act, then reply to the user with a short"
    " summary of what you did, in the language of the user's message."
)

VISION_SYSTEM_PROMPT = (
    "You are a friendly Telegram assistant. The user sent an image (with an"
    " optional caption). Describe or answer based on what the image shows,"
    " briefly and naturally, in the language of the user's message (or the"
    " dialog language if there is no caption)."
)

TRANSLATE_SYSTEM_PROMPT = (
    "Translate Telegram messages into the target language. Return a JSON object "
    "{\"items\": [{\"id\": number, \"translation\": string|null}]} with one entry per input "
    "message. Use null for translation when a message is already in the target language or should "
    "not be translated.\n"
    "Detect each message's source language. If \"only_langs\" is a non-empty list, translate "
    "ONLY messages whose source language is one of those codes and return null for every other "
    "message. Otherwise, return null for any message whose source language is in \"skip_langs\". "
    "Language codes are ISO 639-1 (e.g. ru, en, es)."
)


class TranslationItem(BaseModel):
    """One translated message: the source id and the translation (null = leave untranslated)."""

    id: int = Field(description="The message id, copied verbatim from the input")
    translation: str | None = Field(
        default=None,
        description="Translated text, or null if already in the target language / must not translate",
    )


class TranslationBatch(BaseModel):
    """Structured-output container the translator model fills, one item per input message."""

    items: list[TranslationItem] = Field(default_factory=list)

OUTBOUND_VARIANTS_SYSTEM_PROMPT = (
    "Translate the user's draft into the target language. Produce up to 3 alternative "
    "translations in the user's own voice, using their style profile and recent context. "
    "Output ONLY a JSON array of strings."
)

DETECT_LANG_SYSTEM_PROMPT = (
    "Detect the dominant language of these Telegram messages. Answer with ONLY one of: "
    f"{SUPPORTED_LANG_CODES_PROMPT}. If none apply, answer null."
)


def _strip_json_fence(text: str) -> str:
    raw = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else raw


def make_classifier(model, intents: Sequence[IntentSpec] = ()) -> Callable[[str], Awaitable[str]]:
    """Intent classifier over a plain ainvoke — degradation is predictable: chat."""
    prompt = build_classify_prompt(intents)
    valid = frozenset({"chat", "task", *(spec.name for spec in intents)})

    async def classify(text: str) -> str:
        response = await _ainvoke_with_timeout(
            model,
            [SystemMessage(content=prompt), HumanMessage(content=text)]
        )
        intent = str(response.content).strip().lower().strip(".!\"'")
        if intent not in valid:
            logger.warning("classifier returned %r — falling back to 'chat'", response.content)
            return "chat"
        return intent

    return classify


def _make_prompted_fn(model, system_prompt: str) -> Callable[[list], Awaitable[str]]:
    """A plain ainvoke under a fixed system prompt — chat and vision differ only here."""

    async def call(messages: list) -> str:
        response = await _ainvoke_with_timeout(model, [SystemMessage(content=system_prompt), *messages])
        return str(response.content)

    return call


def make_chat_fn(model) -> Callable[[list], Awaitable[str]]:
    return _make_prompted_fn(model, CHAT_SYSTEM_PROMPT)


def make_vision_fn(model) -> Callable[[list], Awaitable[str]]:
    # мультимодальное сообщение собирает orchestrator — здесь только промпт и вызов
    return _make_prompted_fn(model, VISION_SYSTEM_PROMPT)


SUGGEST_SYSTEM_PROMPT = (
    "You are a writing assistant drafting a reply for a human to review and send"
    " in a Telegram chat. Match the user's OWN voice: tone, length and emoji"
    " habits from the style profile and their past replies. Output ONLY the draft"
    " reply text — no preamble, no quotes — in the language of the conversation."
)


def _render_suggest_payload(context, profile: StyleProfile | None) -> str:
    """Build the user-side prompt: dialog transcript + optional style profile."""
    lines = ["Conversation so far (oldest first):"]
    for msg in context:
        who = "Me" if msg.out else "Them"
        lines.append(f"{who}: {msg.text}")
    if profile is not None:
        lines.append("")
        lines.append("My typical style:")
        lines.append(f"- average reply length: {profile.avg_length:.0f} chars")
        lines.append(f"- emoji per reply: {profile.emoji_freq:.2f}")
        if profile.greetings:
            lines.append(f"- greetings I use: {', '.join(profile.greetings)}")
        if profile.signatures:
            lines.append(f"- sign-offs I use: {', '.join(profile.signatures)}")
        if profile.examples:
            lines.append("- example replies of mine:")
            lines.extend(f"  • {ex}" for ex in profile.examples)
    lines.append("")
    lines.append("Draft my next reply:")
    return "\n".join(lines)


def make_suggest_fn(model) -> Callable:
    """A suggest_fn over a plain ainvoke — injected into the Suggester (#17)."""

    async def suggest(context, profile: StyleProfile | None) -> str:
        payload = _render_suggest_payload(context, profile)
        response = await _ainvoke_with_timeout(
            model,
            [SystemMessage(content=SUGGEST_SYSTEM_PROMPT), HumanMessage(content=payload)]
        )
        return str(response.content).strip()

    return suggest


def _translate_messages(payload: dict) -> list:
    """The system+human messages for one structured translate call."""
    return [
        SystemMessage(content=TRANSLATE_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]


async def _structured_translate(structured_model, payload: dict) -> TranslationBatch | None:
    """One structured call → a TranslationBatch, or None on timeout / error / unusable shape.

    z.ai-style gateways may silently ignore the schema and return prose with HTTP 200; with
    ``with_structured_output`` that surfaces as an exception or a None/garbage value here — either
    way we return None so the caller can fall back to a more lenient method.
    """
    try:
        async with asyncio.timeout(TRANSLATE_TIMEOUT_SECONDS):
            result = await structured_model.ainvoke(_translate_messages(payload))
    except Exception:
        logger.warning("structured translate call failed", exc_info=True)
        return None
    if isinstance(result, TranslationBatch):
        return result
    # some providers hand back a dict when method="json_mode"
    if isinstance(result, dict):
        try:
            return TranslationBatch.model_validate(result)
        except Exception:
            logger.warning("structured translate returned unvalidatable dict: %r", result)
            return None
    logger.warning("structured translate returned unexpected type %s", type(result).__name__)
    return None


def _batch_to_map(batch: TranslationBatch) -> dict[int, str | None]:
    return {int(item.id): item.translation for item in batch.items}


async def probe_structured_method(model) -> str:
    """Detect whether ``model`` honours native json_schema structured output.

    Sends one tiny json_schema request with a predictable input. If it comes back as a usable
    ``TranslationBatch`` with items, the model supports json_schema; otherwise we fall back to
    json_mode (lenient JSON mode). Result is meant to be cached per model by the caller.
    """
    probe_payload = {
        "target_lang": "en",
        "skip_langs": [],
        "only_langs": [],
        "messages": [{"id": 1, "text": "hola"}],
    }
    try:
        schema_model = model.with_structured_output(TranslationBatch, method="json_schema")
    except Exception:
        logger.info("model does not support json_schema structured output; using json_mode")
        return "json_mode"
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            result = await schema_model.ainvoke(_translate_messages(probe_payload))
    except Exception:
        logger.info("json_schema probe failed; using json_mode", exc_info=True)
        return "json_mode"
    batch = result if isinstance(result, TranslationBatch) else None
    if batch is None and isinstance(result, dict):
        try:
            batch = TranslationBatch.model_validate(result)
        except Exception:
            batch = None
    if batch is not None and batch.items:
        logger.info("model supports json_schema structured output")
        return "json_schema"
    logger.info("json_schema probe returned no usable items; using json_mode")
    return "json_mode"


def make_translate_fn(model, method: str = "json_mode") -> Callable:
    """Structured-output translator over ``with_structured_output``; injected into ``Translator``.

    ``method`` is chosen ahead of time by a probe (cached per model). A json_schema run that comes
    back empty/None is retried once with json_mode as a runtime safety net (in case the cached
    probe is stale).
    """
    primary = model.with_structured_output(TranslationBatch, method=method)
    fallback = (
        model.with_structured_output(TranslationBatch, method="json_mode")
        if method == "json_schema"
        else None
    )

    async def translate(messages, target_lang: str, skip_langs=(), only_langs=()) -> dict[int, str | None]:
        payload = {
            "target_lang": target_lang,
            "skip_langs": list(skip_langs),
            "only_langs": list(only_langs),
            "messages": [{"id": int(mid), "text": text} for mid, text in messages],
        }
        batch = await _structured_translate(primary, payload)
        if (batch is None or not batch.items) and fallback is not None:
            logger.info("json_schema translate empty/failed; retrying json_mode")
            batch = await _structured_translate(fallback, payload)
        if batch is None:
            return {}
        return _batch_to_map(batch)

    return translate


def _style_lines(profile: StyleProfile | None) -> list[str]:
    if profile is None:
        return ["No stored style profile."]
    lines = [
        f"average reply length: {profile.avg_length:.0f} chars",
        f"emoji per reply: {profile.emoji_freq:.2f}",
    ]
    if profile.greetings:
        lines.append("greetings: " + ", ".join(profile.greetings))
    if profile.signatures:
        lines.append("sign-offs: " + ", ".join(profile.signatures))
    if profile.examples:
        lines.append("example replies:")
        lines.extend(f"- {ex}" for ex in profile.examples)
    return lines


def make_outbound_variants_fn(model) -> Callable:
    async def variants(
        draft: str,
        target_lang: str,
        profile: StyleProfile | None,
        context: Sequence[ContextMessage],
    ) -> list[str]:
        payload = {
            "target_lang": target_lang,
            "draft": draft,
            "style_profile": _style_lines(profile),
            "context": [{"out": msg.out, "text": msg.text} for msg in context],
        }
        response = await _ainvoke_with_timeout(
            model,
            [
                SystemMessage(content=OUTBOUND_VARIANTS_SYSTEM_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ],
        )
        raw = _strip_json_fence(str(response.content))
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("outbound variants returned non-json content: %r", response.content)
            raise ValueError("outbound variants returned non-json content") from exc
        if not isinstance(parsed, list):
            logger.warning("outbound variants returned non-list content: %r", response.content)
            raise ValueError("outbound variants returned non-list content")
        return [item for item in parsed if isinstance(item, str)]

    return variants


def make_detect_lang_fn(model) -> Callable:
    async def detect(texts: Sequence[str]) -> str | None:
        response = await _ainvoke_with_timeout(
            model,
            [
                SystemMessage(content=DETECT_LANG_SYSTEM_PROMPT),
                HumanMessage(content="\n".join(texts[:10])),
            ],
        )
        code = str(response.content).strip().lower().strip(".!\"'")
        cleaned = clean_supported_lang_code(code)
        if cleaned is not None:
            return cleaned
        if code == "null":
            return None
        logger.warning("language detector returned invalid code %r", response.content)
        return None

    return detect


def build_outbound(store, storage, model_name: str) -> OutboundTranslator:
    model = init_chat_model(model_name)
    return OutboundTranslator(
        store=store,
        storage=storage,
        variants_fn=make_outbound_variants_fn(model),
        detect_lang_fn=make_detect_lang_fn(model),
    )


async def resolve_translate_method(storage, model_name: str, model) -> str:
    """The structured-output method for ``model_name``: cached in kv, else probe once and cache it.

    z.ai-style gateways silently ignore json_schema (HTTP 200, prose), so detection is an ACTIVE
    probe, not a try/except. The result is cached per model so we never probe twice.
    """
    cached = await get_cached_method(storage, model_name)
    if cached is not None:
        return cached
    method = await probe_structured_method(model)
    try:
        await set_cached_method(storage, model_name, method)
    except Exception:
        logger.warning("failed to cache structured method for %s", model_name, exc_info=True)
    return method


def make_self_probing_translate_fn(storage, model_name: str, model) -> Callable:
    """A translate_fn that resolves+caches its structured method on first use, then reuses it.

    Building the Translator stays synchronous (no network at TUI startup); the one-time probe runs
    lazily inside the event loop on the first real translation. After the UI explicitly probes a
    freshly chosen model the method is already in kv, so this path just reads it.
    """
    state: dict[str, Callable] = {}

    async def translate(messages, target_lang, skip_langs=(), only_langs=()):
        fn = state.get("fn")
        if fn is None:
            method = await resolve_translate_method(storage, model_name, model)
            fn = make_translate_fn(model, method)
            state["fn"] = fn
        return await fn(messages, target_lang, skip_langs, only_langs)

    return translate


def build_translator(storage, model_name: str) -> Translator:
    """Build a Translator for ``model_name``. The structured-output method is probed lazily on the
    first translation (and cached per model in kv), so construction stays synchronous and fast."""
    model = init_chat_model(model_name)
    return Translator(
        storage=storage,
        translate_fn=make_self_probing_translate_fn(storage, model_name, model),
    )


async def build_translator_with_probe(storage, model_name: str) -> Translator:
    """Build a Translator and probe the model's structured method UP FRONT (caching it in kv).

    For the TUI 'pick a model' flow, which runs inside the event loop: constructing the model can
    raise on a bad name / missing key, and the probe surfaces support immediately so the first real
    translation isn't slowed by detection. Raises if the model can't be constructed.
    """
    model = init_chat_model(model_name)
    await resolve_translate_method(storage, model_name, model)  # probe + cache now
    return Translator(
        storage=storage,
        translate_fn=make_self_probing_translate_fn(storage, model_name, model),
    )


def build_suggester(client, cfg: AgentConfig, storage=None) -> Suggester:
    model = init_chat_model(cfg.model)
    return Suggester(
        client=client,
        suggest_fn=make_suggest_fn(model),
        storage=storage,
        history_limit=cfg.suggest_history_limit,
    )


def build_orchestrator(client, cfg: AgentConfig) -> Orchestrator:
    model = init_chat_model(cfg.model)
    # без TG_AGENT_VISION_MODEL картинки идут в основную модель —
    # тогда она должна быть мультимодальной (см. .env.example)
    vision_model = init_chat_model(cfg.vision_model) if cfg.vision_model else model
    task_agent = create_deep_agent(
        model=model,
        tools=[
            *make_telegram_tools(
                client, factory_url=cfg.factory_url, factory_password=cfg.factory_password
            ),
            build_search_fn(cfg.search_provider),
        ],
        system_prompt=TASK_SYSTEM_PROMPT,
    )
    return Orchestrator(
        classify_fn=make_classifier(model, cfg.intents),
        chat_fn=make_chat_fn(model),
        task_agent=task_agent,
        vision_fn=make_vision_fn(vision_model),
        intents=cfg.intents,
    )
