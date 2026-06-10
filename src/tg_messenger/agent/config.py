"""Agent settings from the environment (stdlib-only, no LLM imports).

``ValueError`` messages are user-facing — the CLI wraps them into
``ClickException`` verbatim.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

SEARCH_PROVIDERS = ("duckduckgo", "tavily", "exa", "brave")


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

        return cls(
            model=model,
            allow_all=allow_all,
            allow_ids=frozenset(allow_ids),
            allow_usernames=frozenset(allow_usernames),
            search_provider=search,
        )
