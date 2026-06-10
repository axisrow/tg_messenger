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


def build_orchestrator(client, cfg: AgentConfig) -> Orchestrator:
    model = init_chat_model(cfg.model)
    # без TG_AGENT_VISION_MODEL картинки идут в основную модель —
    # тогда она должна быть мультимодальной (см. .env.example)
    vision_model = init_chat_model(cfg.vision_model) if cfg.vision_model else model
    task_agent = create_deep_agent(
        model=model,
        tools=[*make_telegram_tools(client), build_search_fn(cfg.search_provider)],
        system_prompt=TASK_SYSTEM_PROMPT,
    )
    return Orchestrator(
        classify_fn=make_classifier(model, cfg.intents),
        chat_fn=make_chat_fn(model),
        task_agent=task_agent,
        vision_fn=make_vision_fn(vision_model),
        intents=cfg.intents,
    )
