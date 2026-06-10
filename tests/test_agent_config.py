"""Цикл 9: AgentConfig.from_env — чтение и валидация настроек агента (stdlib-only)."""

import json

import pytest

from tg_messenger.agent.config import (
    SEARCH_PROVIDERS,
    AgentConfig,
    IntentSpec,
    langsmith_tracing_enabled,
    load_intents,
)

VALID_ENV = {
    "TG_AGENT_MODEL": "anthropic:claude-sonnet-4-6",
    "TG_AGENT_ALLOWLIST": "123, @Ann, bob",
    "TG_AGENT_SEARCH": "tavily",
}


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch, tmp_path):
    # from_env подхватывает ./agent.json из cwd — тесты не должны видеть файл разработчика
    monkeypatch.chdir(tmp_path)


def test_valid_env_parses_all_fields():
    cfg = AgentConfig.from_env(VALID_ENV)
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.allow_all is False
    assert cfg.allow_ids == frozenset({123})
    # usernames нормализуются: без @, lower-case
    assert cfg.allow_usernames == frozenset({"ann", "bob"})
    assert cfg.search_provider == "tavily"


def test_allowlist_star_means_everyone():
    cfg = AgentConfig.from_env({**VALID_ENV, "TG_AGENT_ALLOWLIST": "*"})
    assert cfg.allow_all is True
    assert cfg.allow_ids == frozenset()
    assert cfg.allow_usernames == frozenset()


def test_missing_model_raises():
    env = {k: v for k, v in VALID_ENV.items() if k != "TG_AGENT_MODEL"}
    with pytest.raises(ValueError, match="TG_AGENT_MODEL"):
        AgentConfig.from_env(env)


def test_model_without_colon_raises_with_format_example():
    with pytest.raises(ValueError, match="provider:model"):
        AgentConfig.from_env({**VALID_ENV, "TG_AGENT_MODEL": "claude-sonnet-4-6"})


@pytest.mark.parametrize("allowlist", [None, "", "  ", " , "])
def test_missing_or_empty_allowlist_raises_mentioning_star(allowlist):
    env = dict(VALID_ENV)
    if allowlist is None:
        del env["TG_AGENT_ALLOWLIST"]
    else:
        env["TG_AGENT_ALLOWLIST"] = allowlist
    with pytest.raises(ValueError, match=r"TG_AGENT_ALLOWLIST.*\*"):
        AgentConfig.from_env(env)


def test_unknown_search_provider_raises_listing_known_ones():
    with pytest.raises(ValueError, match="duckduckgo"):
        AgentConfig.from_env({**VALID_ENV, "TG_AGENT_SEARCH": "yahoo"})


def test_search_defaults_to_duckduckgo():
    env = {k: v for k, v in VALID_ENV.items() if k != "TG_AGENT_SEARCH"}
    cfg = AgentConfig.from_env(env)
    assert cfg.search_provider == "duckduckgo"


def test_search_providers_tuple_is_complete():
    assert set(SEARCH_PROVIDERS) == {"duckduckgo", "tavily", "exa", "brave"}


def test_from_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("TG_AGENT_MODEL", "openai:gpt-5.4")
    monkeypatch.setenv("TG_AGENT_ALLOWLIST", "42")
    monkeypatch.delenv("TG_AGENT_SEARCH", raising=False)
    cfg = AgentConfig.from_env()
    assert cfg.model == "openai:gpt-5.4"
    assert cfg.allow_ids == frozenset({42})


def test_non_numeric_entry_without_at_is_treated_as_username():
    cfg = AgentConfig.from_env({**VALID_ENV, "TG_AGENT_ALLOWLIST": "@User_Name"})
    assert cfg.allow_usernames == frozenset({"user_name"})
    assert cfg.allow_ids == frozenset()


@pytest.mark.parametrize("allowlist", ["*, @ann", "123, *"])
def test_star_mixed_with_other_entries_raises(allowlist):
    # раньше '*' молча уходил в usernames и не матчился никогда — агент блокировал всех
    with pytest.raises(ValueError, match=r"\*"):
        AgentConfig.from_env({**VALID_ENV, "TG_AGENT_ALLOWLIST": allowlist})


# --- Цикл 17: langsmith_tracing_enabled — трассировка LangGraph/LangChain env-ами ---


@pytest.mark.parametrize("env", [{}, {"LANGSMITH_TRACING": "false"}, {"LANGSMITH_TRACING": "0"}])
def test_langsmith_tracing_off_by_default(env):
    assert langsmith_tracing_enabled(env) is False


@pytest.mark.parametrize("flag", ["true", "True", "1", "yes"])
def test_langsmith_tracing_on_with_key(flag):
    env = {"LANGSMITH_TRACING": flag, "LANGSMITH_API_KEY": "lsv2-key"}
    assert langsmith_tracing_enabled(env) is True


def test_langsmith_tracing_on_without_key_fails_fast():
    # иначе langsmith молча сыпал бы фоновые ошибки на каждый трейс
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        langsmith_tracing_enabled({"LANGSMITH_TRACING": "true"})


def test_langsmith_tracing_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-key")
    assert langsmith_tracing_enabled() is True


# --- Цикл 22: TG_AGENT_VISION_MODEL ---


def test_vision_model_unset_is_none():
    assert AgentConfig.from_env(VALID_ENV).vision_model is None


def test_vision_model_parsed():
    cfg = AgentConfig.from_env({**VALID_ENV, "TG_AGENT_VISION_MODEL": "openai:gpt-5-vision"})
    assert cfg.vision_model == "openai:gpt-5-vision"


def test_vision_model_without_colon_raises_with_format_example():
    with pytest.raises(ValueError, match=r"TG_AGENT_VISION_MODEL.*provider:model"):
        AgentConfig.from_env({**VALID_ENV, "TG_AGENT_VISION_MODEL": "gpt-5-vision"})


# --- Цикл 23: кастомные интенты из agent.json ---

TRANSLATE = {
    "name": "translate",
    "description": "просит перевести текст",
    "pipeline": "chat",
    "system_prompt": "Ты переводчик.",
}


def write_intents(path, intents):
    path.write_text(json.dumps({"intents": intents}, ensure_ascii=False), encoding="utf-8")
    return path


def test_no_config_file_means_builtin_intents_only():
    assert AgentConfig.from_env(VALID_ENV).intents == ()


def test_agent_json_in_cwd_is_picked_up(tmp_path):
    write_intents(tmp_path / "agent.json", [TRANSLATE])
    cfg = AgentConfig.from_env(VALID_ENV)
    assert cfg.intents == (
        IntentSpec(name="translate", description="просит перевести текст",
                   pipeline="chat", system_prompt="Ты переводчик."),
    )


def test_explicit_config_path_wins_over_cwd(tmp_path):
    write_intents(tmp_path / "agent.json", [TRANSLATE])
    other = write_intents(tmp_path / "other.json",
                          [{"name": "recipe", "description": "просит рецепт", "pipeline": "task"}])
    cfg = AgentConfig.from_env({**VALID_ENV, "TG_AGENT_CONFIG": str(other)})
    assert [i.name for i in cfg.intents] == ["recipe"]
    assert cfg.intents[0].system_prompt is None  # опционален


def test_explicit_missing_config_path_raises(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ValueError, match="nope.json"):
        AgentConfig.from_env({**VALID_ENV, "TG_AGENT_CONFIG": str(missing)})


def test_broken_json_raises_with_path(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"agent\.json"):
        load_intents(path)


@pytest.mark.parametrize("payload", [
    ["not", "an", "object"],          # корень — не объект
    {"intents": {"name": "x"}},        # intents — не список
    {"intents": ["строка"]},           # элемент — не объект
])
def test_wrong_top_level_shape_raises(tmp_path, payload):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=r"agent\.json"):
        load_intents(path)


@pytest.mark.parametrize("bad,why", [
    ({**TRANSLATE, "name": "chat"}, "встроенное имя"),
    ({**TRANSLATE, "name": "vision"}, "служебное имя узла"),
    ({**TRANSLATE, "name": "Two Words"}, "имя — не одно слово в нижнем регистре"),
    ({**TRANSLATE, "name": ""}, "пустое имя"),
    ({**TRANSLATE, "pipeline": "workflow"}, "неизвестный pipeline"),
    ({**TRANSLATE, "description": "  "}, "пустое описание"),
    ({**TRANSLATE, "extra_key": 1}, "опечатка в ключе — fail-fast"),
    ({**TRANSLATE, "system_prompt": 42}, "system_prompt — не строка"),
])
def test_invalid_intent_raises(tmp_path, bad, why):
    path = write_intents(tmp_path / "agent.json", [bad])
    with pytest.raises(ValueError, match=r"agent\.json"):
        load_intents(path)


def test_duplicate_intent_names_raise(tmp_path):
    path = write_intents(tmp_path / "agent.json", [TRANSLATE, TRANSLATE])
    with pytest.raises(ValueError, match="translate"):
        load_intents(path)
