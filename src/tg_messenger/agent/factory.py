"""Production wiring of the agent — the ONLY module touching the LLM stack.

Everything heavy (init_chat_model, create_deep_agent) is imported and called
here, so version drift in langchain/deepagents stays contained in one file,
and tests stub these module-level names directly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from tg_messenger.agent.config import AgentConfig, IntentSpec
from tg_messenger.agent.orchestrator import Orchestrator
from tg_messenger.agent.search import build_search_fn
from tg_messenger.agent.suggest import StyleProfile, Suggester
from tg_messenger.agent.tools import make_telegram_tools

logger = logging.getLogger(__name__)


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


CLASSIFY_SYSTEM_PROMPT = build_classify_prompt()

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


def make_classifier(model, intents: Sequence[IntentSpec] = ()) -> Callable[[str], Awaitable[str]]:
    """Intent classifier over a plain ainvoke — degradation is predictable: chat."""
    prompt = build_classify_prompt(intents)
    valid = frozenset({"chat", "task", *(spec.name for spec in intents)})

    async def classify(text: str) -> str:
        response = await model.ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content=text)]
        )
        intent = str(response.content).strip().lower().strip(".!\"'")
        if intent not in valid:
            logger.warning("classifier returned %r — falling back to 'chat'", response.content)
            return "chat"
        return intent

    return classify


def make_chat_fn(model) -> Callable[[list], Awaitable[str]]:
    async def chat(messages: list) -> str:
        response = await model.ainvoke([SystemMessage(content=CHAT_SYSTEM_PROMPT), *messages])
        return str(response.content)

    return chat


def make_vision_fn(model) -> Callable[[list], Awaitable[str]]:
    # мультимодальное сообщение собирает orchestrator — здесь только промпт и вызов
    async def vision(messages: list) -> str:
        response = await model.ainvoke([SystemMessage(content=VISION_SYSTEM_PROMPT), *messages])
        return str(response.content)

    return vision


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
        response = await model.ainvoke(
            [SystemMessage(content=SUGGEST_SYSTEM_PROMPT), HumanMessage(content=payload)]
        )
        return str(response.content).strip()

    return suggest


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
