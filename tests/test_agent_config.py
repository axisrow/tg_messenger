"""Цикл 9: AgentConfig.from_env — чтение и валидация настроек агента (stdlib-only)."""

import json
import sys
from types import ModuleType

import pytest

from tg_messenger.agent.config import (
    SEARCH_PROVIDERS,
    AgentConfig,
    IntentSpec,
    flush_tracers,
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


@pytest.mark.parametrize("allowlist", [None, "", "  ", " , "])
def test_allowlist_can_be_omitted_for_suggester(allowlist):
    env = dict(VALID_ENV)
    if allowlist is None:
        del env["TG_AGENT_ALLOWLIST"]
    else:
        env["TG_AGENT_ALLOWLIST"] = allowlist
    cfg = AgentConfig.from_env(env, require_allowlist=False)
    assert cfg.allow_all is False
    assert cfg.allow_ids == frozenset()
    assert cfg.allow_usernames == frozenset()


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


# --- #168: flush_tracers — досылаем буфер трейсов LangSmith перед выходом ---

_TRACER_MODULE = "langchain_core.tracers.langchain"


def _fake_tracer_module(monkeypatch, fn):
    """Подменяем langchain_core.tracers.langchain на фейк с шпионом wait_for_all_tracers."""
    mod = ModuleType(_TRACER_MODULE)
    mod.wait_for_all_tracers = fn
    monkeypatch.setitem(sys.modules, _TRACER_MODULE, mod)


def test_flush_tracers_calls_wait_for_all_tracers_once(monkeypatch):
    calls = []
    _fake_tracer_module(monkeypatch, lambda: calls.append(1))
    flush_tracers()
    assert calls == [1]


def test_flush_tracers_no_op_when_langchain_absent(monkeypatch):
    # без [agent]-экстры импорт падает — flush_tracers молча ничего не делает, не падает
    monkeypatch.setitem(sys.modules, _TRACER_MODULE, None)  # импорт → ImportError
    flush_tracers()  # не должно бросить


def test_flush_tracers_swallows_errors(monkeypatch):
    # сбой досыла на shutdown не должен ронять процесс — логируем и проглатываем
    def boom():
        raise RuntimeError("tracer backend down")

    _fake_tracer_module(monkeypatch, boom)
    flush_tracers()  # не должно бросить


def test_flush_tracers_returns_within_deadline_when_flush_blocks(monkeypatch):
    # #168 (Codex): wait_for_all_tracers → Client.flush(timeout=None) ждёт ВЕЧНО; на shutdown
    # из finally это вешает Ctrl+C/стоп сервера. flush_tracers ОБЯЗАН вернуться по дедлайну.
    import threading
    import time

    release = threading.Event()

    def blocking():
        release.wait()  # имитируем зависший tracer-воркер / деградацию сети

    _fake_tracer_module(monkeypatch, blocking)
    monkeypatch.setenv("TG_TRACE_FLUSH_TIMEOUT", "0.2")
    start = time.monotonic()
    flush_tracers()  # не должно зависнуть
    elapsed = time.monotonic() - start
    release.set()  # отпускаем фоновый поток, чтобы тест не оставлял висящих демонов
    assert elapsed < 2.0, f"flush_tracers blocked for {elapsed:.2f}s past its deadline"


# --- Цикл 22: TG_AGENT_VISION_MODEL ---


def test_vision_model_unset_is_none():
    assert AgentConfig.from_env(VALID_ENV).vision_model is None


def test_vision_model_parsed():
    cfg = AgentConfig.from_env({**VALID_ENV, "TG_AGENT_VISION_MODEL": "openai:gpt-5-vision"})
    assert cfg.vision_model == "openai:gpt-5-vision"


def test_vision_model_without_colon_raises_with_format_example():
    with pytest.raises(ValueError, match=r"TG_AGENT_VISION_MODEL.*provider:model"):
        AgentConfig.from_env({**VALID_ENV, "TG_AGENT_VISION_MODEL": "gpt-5-vision"})


# --- #158: TG_SUGGEST_MODEL (a separate, faster model for the suggester) ---


def test_suggest_model_unset_is_none():
    assert AgentConfig.from_env(VALID_ENV).suggest_model is None


def test_suggest_model_parsed():
    cfg = AgentConfig.from_env({**VALID_ENV, "TG_SUGGEST_MODEL": "openai:glm-5-turbo"})
    assert cfg.suggest_model == "openai:glm-5-turbo"


def test_suggest_model_without_colon_raises_with_format_example():
    with pytest.raises(ValueError, match=r"TG_SUGGEST_MODEL.*provider:model"):
        AgentConfig.from_env({**VALID_ENV, "TG_SUGGEST_MODEL": "glm-5-turbo"})


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
