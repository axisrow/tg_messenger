"""Agent settings from the environment (stdlib-only, no LLM imports).

``ValueError`` messages are user-facing — the CLI wraps them into
``ClickException`` verbatim.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

SEARCH_PROVIDERS = ("duckduckgo", "tavily", "exa", "brave")

INTENT_PIPELINES = ("chat", "task")
# встроенные интенты + имена узлов графа — кастомным интентам они запрещены
RESERVED_INTENT_NAMES = frozenset({"chat", "task", "classify", "vision", "route"})
_INTENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")  # классификатор отвечает одним словом
_INTENT_KEYS = frozenset({"name", "description", "pipeline", "system_prompt"})


@dataclass(frozen=True)
class IntentSpec:
    name: str
    description: str  # критерий для LLM-классификатора
    pipeline: str  # куда направлять: "chat" | "task"
    system_prompt: str | None = None  # доп. инструкция этого интента


def load_intents(path: str | Path) -> tuple[IntentSpec, ...]:
    """Parse the agent.json intents file; every problem is a user-facing ValueError."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"agent config {path}: file not found.") from None
    except OSError as exc:
        raise ValueError(f"agent config {path}: cannot read: {exc}.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"agent config {path}: invalid JSON: {exc}.") from exc

    if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
        raise ValueError(f'agent config {path}: expected an object {{"intents": [...]}}.')

    specs: list[IntentSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(data["intents"]):
        where = f"agent config {path}: intents[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: expected an object.")
        unknown = set(item) - _INTENT_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys: {', '.join(sorted(unknown))}.")
        name = item.get("name")
        if not isinstance(name, str) or not _INTENT_NAME_RE.fullmatch(name):
            raise ValueError(
                f"{where}: 'name' must be a single lowercase word"
                f" matching {_INTENT_NAME_RE.pattern!r}, got {name!r}."
            )
        if name in RESERVED_INTENT_NAMES:
            raise ValueError(f"{where}: name {name!r} is reserved.")
        if name in seen:
            raise ValueError(f"{where}: duplicate name {name!r}.")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{where}: 'description' must be a non-empty string.")
        pipeline = item.get("pipeline")
        if pipeline not in INTENT_PIPELINES:
            raise ValueError(
                f"{where}: 'pipeline' must be one of: {', '.join(INTENT_PIPELINES)}, got {pipeline!r}."
            )
        system_prompt = item.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ValueError(f"{where}: 'system_prompt' must be a string.")
        seen.add(name)
        specs.append(IntentSpec(name=name, description=description.strip(),
                                pipeline=pipeline, system_prompt=system_prompt))
    return tuple(specs)


def langsmith_tracing_enabled(env: Mapping[str, str] | None = None) -> bool:
    """LangSmith-трассировка включена? langchain/langgraph читают эти же env сами.

    Включено без ключа — ValueError: иначе langsmith молча сыпал бы фоновые
    ошибки на каждый трейс.
    """
    if env is None:
        env = os.environ
    if (env.get("LANGSMITH_TRACING") or "").strip().lower() not in ("1", "true", "yes"):
        return False
    if not (env.get("LANGSMITH_API_KEY") or "").strip():
        raise ValueError(
            "LANGSMITH_TRACING is on, but LANGSMITH_API_KEY is not set —"
            " add a key from https://smith.langchain.com or disable tracing."
        )
    return True


@dataclass(frozen=True)
class AgentConfig:
    model: str
    allow_all: bool
    allow_ids: frozenset[int] = field(default_factory=frozenset)
    allow_usernames: frozenset[str] = field(default_factory=frozenset)
    search_provider: str = "duckduckgo"
    vision_model: str | None = None  # None — картинки идут в основную модель
    intents: tuple[IntentSpec, ...] = ()  # кастомные интенты из agent.json

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        if env is None:
            env = os.environ

        model = (env.get("TG_AGENT_MODEL") or "").strip()
        if not model:
            raise ValueError(
                "TG_AGENT_MODEL is not set — expected 'provider:model', e.g. 'anthropic:claude-sonnet-4-6'."
            )
        if ":" not in model:
            raise ValueError(
                f"TG_AGENT_MODEL={model!r} is not in 'provider:model' format,"
                " e.g. 'anthropic:claude-sonnet-4-6' or 'openai:gpt-5.4'."
            )

        raw_allow = (env.get("TG_AGENT_ALLOWLIST") or "").strip()
        entries = [e.strip() for e in raw_allow.split(",") if e.strip()]
        if not entries:
            # no safe silent default: replying to everyone must be an explicit '*'
            raise ValueError(
                "TG_AGENT_ALLOWLIST is not set — use '*' to reply to everyone,"
                " or a comma-separated list of numeric ids / @usernames."
            )
        if "*" in entries and len(entries) > 1:
            # иначе '*' молча уехал бы в usernames и не сматчился никогда
            raise ValueError(
                "TG_AGENT_ALLOWLIST: '*' (everyone) cannot be combined with other entries."
            )
        allow_all = entries == ["*"]
        allow_ids: set[int] = set()
        allow_usernames: set[str] = set()
        if not allow_all:
            for entry in entries:
                if entry.lstrip("-").isdigit():
                    allow_ids.add(int(entry))
                else:
                    allow_usernames.add(entry.lstrip("@").lower())

        search = (env.get("TG_AGENT_SEARCH") or "duckduckgo").strip().lower()
        if search not in SEARCH_PROVIDERS:
            raise ValueError(
                f"TG_AGENT_SEARCH={search!r} is unknown — choose one of: {', '.join(SEARCH_PROVIDERS)}."
            )

        vision_model = (env.get("TG_AGENT_VISION_MODEL") or "").strip() or None
        if vision_model is not None and ":" not in vision_model:
            raise ValueError(
                f"TG_AGENT_VISION_MODEL={vision_model!r} is not in 'provider:model' format,"
                " e.g. 'openai:gpt-5.4' or 'anthropic:claude-sonnet-4-6'."
            )

        config_path = (env.get("TG_AGENT_CONFIG") or "").strip()
        if config_path:
            intents = load_intents(config_path)  # явно указанный файл обязан существовать
        elif Path("agent.json").is_file():
            intents = load_intents("agent.json")
        else:
            intents = ()

        return cls(
            model=model,
            allow_all=allow_all,
            allow_ids=frozenset(allow_ids),
            allow_usernames=frozenset(allow_usernames),
            search_provider=search,
            vision_model=vision_model,
            intents=intents,
        )
