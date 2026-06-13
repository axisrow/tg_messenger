"""Цикл 14: factory (сборка продакшен-частей) и CLI-команда `agent`.

LLM-стек не вызывается: init_chat_model/create_deep_agent патчатся на фейки,
CLI тестируется через CliRunner со стабами make_client/make_agent_runner.
"""

import asyncio
import logging
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from tg_messenger.cli import main as cli_main

# --- factory (нужен установленный agent-extra; без него — skip) ---

deepagents = pytest.importorskip("deepagents")

from tg_messenger.agent import factory  # noqa: E402
from tg_messenger.agent.config import AgentConfig, IntentSpec  # noqa: E402
from tg_messenger.agent.orchestrator import Orchestrator  # noqa: E402


def make_cfg(**kw):
    defaults = dict(model="anthropic:claude-x", allow_all=True, search_provider="duckduckgo")
    defaults.update(kw)
    return AgentConfig(**defaults)


class FakeModel:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.reply)


class HangingModel:
    async def ainvoke(self, messages):
        await asyncio.Event().wait()


def test_build_orchestrator_wires_model_and_tools(monkeypatch):
    init_calls = []
    deep_calls = []
    fake_model = FakeModel()
    monkeypatch.setattr(factory, "init_chat_model", lambda spec: init_calls.append(spec) or fake_model)
    monkeypatch.setattr(
        factory, "create_deep_agent",
        lambda **kw: deep_calls.append(kw) or SimpleNamespace(ainvoke=None),
    )
    orch = factory.build_orchestrator(client=SimpleNamespace(), cfg=make_cfg())
    assert isinstance(orch, Orchestrator)
    assert init_calls == ["anthropic:claude-x"]
    (kw,) = deep_calls
    assert kw["model"] is fake_model
    assert kw["system_prompt"].strip()
    tool_names = {t.__name__ for t in kw["tools"]}
    assert tool_names == {
        "send_telegram_message", "read_telegram_history", "list_telegram_dialogs", "web_search",
    }


@pytest.mark.parametrize("raw,expected", [
    ("task", "task"),
    (" Task.\n", "task"),
    ("chat", "chat"),
    ("CHAT!", "chat"),
])
async def test_classifier_parses_model_reply(raw, expected):
    classify = factory.make_classifier(FakeModel(reply=raw))
    assert await classify("что-нибудь") == expected


async def test_classifier_garbage_falls_back_to_chat_with_warning(caplog):
    classify = factory.make_classifier(FakeModel(reply="I think maybe..."))
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.factory"):
        assert await classify("что-нибудь") == "chat"
    assert any("chat" in r.message for r in caplog.records)  # не молча


async def test_classifier_sends_user_text_to_model():
    model = FakeModel(reply="chat")
    classify = factory.make_classifier(model)
    await classify("привет, бот")
    (messages,) = model.calls
    assert messages[-1].content == "привет, бот"


async def test_classifier_times_out(monkeypatch):
    monkeypatch.setattr(factory, "MODEL_CALL_TIMEOUT_SECONDS", 0.01)
    classify = factory.make_classifier(HangingModel())
    with pytest.raises(TimeoutError):
        await classify("привет")


async def test_chat_fn_returns_content_and_keeps_history():
    model = FakeModel(reply="ответ")
    chat = factory.make_chat_fn(model)
    history = [SimpleNamespace(content="раньше"), SimpleNamespace(content="сейчас")]
    assert await chat(history) == "ответ"
    (messages,) = model.calls
    assert messages[-2:] == history  # история ушла модели целиком, в конце


async def test_chat_fn_times_out(monkeypatch):
    monkeypatch.setattr(factory, "MODEL_CALL_TIMEOUT_SECONDS", 0.01)
    chat = factory.make_chat_fn(HangingModel())
    with pytest.raises(TimeoutError):
        await chat([])


# --- Цикл 22: vision-функция и vision-модель ---


async def test_vision_fn_uses_vision_prompt_and_returns_content():
    model = FakeModel(reply="на фото кот")
    vision = factory.make_vision_fn(model)
    history = [SimpleNamespace(content="что тут?")]
    assert await vision(history) == "на фото кот"
    (messages,) = model.calls
    assert messages[0].content == factory.VISION_SYSTEM_PROMPT
    assert messages[-1:] == history


def test_build_orchestrator_with_dedicated_vision_model(monkeypatch):
    init_calls = []
    monkeypatch.setattr(factory, "init_chat_model", lambda spec: init_calls.append(spec) or FakeModel())
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kw: SimpleNamespace(ainvoke=None))
    orch = factory.build_orchestrator(
        client=SimpleNamespace(), cfg=make_cfg(vision_model="openai:gpt-5-vision"),
    )
    assert init_calls == ["anthropic:claude-x", "openai:gpt-5-vision"]
    assert orch._vision_fn is not None


def test_build_orchestrator_reuses_main_model_for_vision(monkeypatch):
    init_calls = []
    monkeypatch.setattr(factory, "init_chat_model", lambda spec: init_calls.append(spec) or FakeModel())
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kw: SimpleNamespace(ainvoke=None))
    orch = factory.build_orchestrator(client=SimpleNamespace(), cfg=make_cfg())
    assert init_calls == ["anthropic:claude-x"]  # одна модель на всё
    assert orch._vision_fn is not None  # vision работает и без отдельной модели


# --- Цикл 95: make_suggest_fn (черновик ответа) + build_suggester ---


async def test_make_suggest_fn_builds_draft_from_context():
    from tg_messenger.agent.suggest import ContextMessage, StyleProfile

    model = FakeModel(reply="вот мой черновик")
    suggest_fn = factory.make_suggest_fn(model)
    context = [
        ContextMessage(out=False, text="привет"),
        ContextMessage(out=True, text="о, привет!"),
        ContextMessage(out=False, text="как дела?"),
    ]
    profile = StyleProfile(avg_length=10.0, emoji_freq=0.2, examples=["ок"])
    draft = await suggest_fn(context, profile)
    assert draft == "вот мой черновик"
    (messages,) = model.calls
    # история ушла модели; профиль попал в промпт
    rendered = "\n".join(str(m.content) for m in messages)
    assert "привет" in rendered and "как дела?" in rendered


async def test_make_suggest_fn_works_without_profile():
    from tg_messenger.agent.suggest import ContextMessage

    model = FakeModel(reply="черновик")
    suggest_fn = factory.make_suggest_fn(model)
    draft = await suggest_fn([ContextMessage(out=False, text="hi")], None)
    assert draft == "черновик"


async def test_make_suggest_fn_times_out(monkeypatch):
    from tg_messenger.agent.suggest import ContextMessage

    monkeypatch.setattr(factory, "MODEL_CALL_TIMEOUT_SECONDS", 0.01)
    suggest_fn = factory.make_suggest_fn(HangingModel())
    with pytest.raises(TimeoutError):
        await suggest_fn([ContextMessage(out=False, text="hi")], None)


def test_build_suggester_wires_model(monkeypatch):
    from tg_messenger.agent.suggest import Suggester

    monkeypatch.setattr(factory, "init_chat_model", lambda spec: FakeModel())
    suggester = factory.build_suggester(
        client=SimpleNamespace(), cfg=make_cfg(), storage=SimpleNamespace()
    )
    assert isinstance(suggester, Suggester)


async def test_make_translate_fn_parses_fenced_json_and_null():
    model = FakeModel(reply='```json\n[{"id": 1, "translation": "привет"}, {"id": 2, "translation": null}]\n```')
    translate_fn = factory.make_translate_fn(model)
    result = await translate_fn([(1, "hello"), (2, "ok")], "ru")
    assert result == {1: "привет", 2: None}
    rendered = "\n".join(str(m.content) for m in model.calls[0])
    assert "target_lang" in rendered


async def test_make_translate_fn_garbage_returns_empty(caplog):
    translate_fn = factory.make_translate_fn(FakeModel(reply="not json"))
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.factory"):
        assert await translate_fn([(1, "hello")], "ru") == {}
    assert any("translator returned non-json" in rec.message for rec in caplog.records)


async def test_make_translate_fn_times_out(monkeypatch):
    monkeypatch.setattr(factory, "MODEL_CALL_TIMEOUT_SECONDS", 0.01)
    translate_fn = factory.make_translate_fn(HangingModel())
    with pytest.raises(TimeoutError):
        await translate_fn([(1, "hello")], "ru")


async def test_make_outbound_variants_fn_parses_array():
    variants_fn = factory.make_outbound_variants_fn(FakeModel(reply='```json\n["hi", "hello"]\n```'))
    result = await variants_fn("привет", "en", None, [])
    assert result == ["hi", "hello"]


async def test_make_outbound_variants_fn_garbage_raises(caplog):
    variants_fn = factory.make_outbound_variants_fn(FakeModel(reply="nope"))
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.factory"):
        with pytest.raises(ValueError):
            await variants_fn("привет", "en", None, [])
    assert any("outbound variants returned non-json" in rec.message for rec in caplog.records)


async def test_make_outbound_variants_fn_times_out(monkeypatch):
    monkeypatch.setattr(factory, "MODEL_CALL_TIMEOUT_SECONDS", 0.01)
    variants_fn = factory.make_outbound_variants_fn(HangingModel())
    with pytest.raises(TimeoutError):
        await variants_fn("привет", "en", None, [])


async def test_make_detect_lang_fn_validates_code():
    model = FakeModel(reply="EN.")
    assert await factory.make_detect_lang_fn(model)(["hello"]) == "en"
    rendered = "\n".join(str(m.content) for m in model.calls[0])
    assert "ru, en, es, uk, ja, zh, ko, ar, he, el, th" in rendered
    assert await factory.make_detect_lang_fn(FakeModel(reply="FR."))(["bonjour"]) is None
    assert await factory.make_detect_lang_fn(FakeModel(reply="English"))(["hello"]) is None


async def test_make_detect_lang_fn_times_out(monkeypatch):
    monkeypatch.setattr(factory, "MODEL_CALL_TIMEOUT_SECONDS", 0.01)
    detect_fn = factory.make_detect_lang_fn(HangingModel())
    with pytest.raises(TimeoutError):
        await detect_fn(["hello"])


# --- Цикл 25: классификатор по списку интентов + видимость конфига в CLI ---

RECIPE = IntentSpec(name="recipe", description="просит рецепт блюда", pipeline="chat",
                    system_prompt="Ты повар.")


def test_build_classify_prompt_includes_custom_intents():
    prompt = factory.build_classify_prompt((RECIPE,))
    assert "recipe" in prompt and "просит рецепт блюда" in prompt
    assert "task" in prompt and "chat" in prompt  # встроенные остаются


async def test_classifier_accepts_custom_intent_name():
    classify = factory.make_classifier(FakeModel(reply=" Recipe.\n"), intents=(RECIPE,))
    assert await classify("как сварить борщ") == "recipe"


async def test_classifier_unknown_word_falls_back_to_chat_with_intents(caplog):
    classify = factory.make_classifier(FakeModel(reply="pizza"), intents=(RECIPE,))
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.factory"):
        assert await classify("что-нибудь") == "chat"
    assert any("chat" in r.message for r in caplog.records)


def test_build_orchestrator_passes_intents(monkeypatch):
    monkeypatch.setattr(factory, "init_chat_model", lambda spec: FakeModel())
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kw: SimpleNamespace(ainvoke=None))
    orch = factory.build_orchestrator(client=SimpleNamespace(), cfg=make_cfg(intents=(RECIPE,)))
    assert "recipe" in orch._routes  # узел кастомного интента есть в графе


def test_make_agent_runner_announces_intents_and_vision(monkeypatch, tmp_path, capsys):
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / "agent.json").write_text(json.dumps({"intents": [
        {"name": "recipe", "description": "просит рецепт", "pipeline": "chat"},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TG_AGENT_MODEL", "anthropic:claude-x")
    monkeypatch.setenv("TG_AGENT_ALLOWLIST", "*")
    monkeypatch.setenv("TG_AGENT_VISION_MODEL", "openai:gpt-5-vision")
    monkeypatch.setattr("tg_messenger.agent.factory.build_orchestrator",
                        lambda client, cfg: SimpleNamespace())
    cli_main.make_agent_runner(SimpleNamespace())
    out = capsys.readouterr().out
    assert "openai:gpt-5-vision" in out  # vision-модель видна на старте
    assert "recipe" in out  # и загруженные кастомные интенты тоже


# --- CLI-команда agent ---

class StubClient:
    def __init__(self, **kw):
        self.connected = False
        self.authorized = True

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_authorized(self):
        return self.authorized


class StubRunner:
    def __init__(self):
        self.runs = 0
        self.interrupt = False

    async def run(self):
        self.runs += 1
        if self.interrupt:
            raise KeyboardInterrupt


@pytest.fixture
def agent_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # CLI грузит .env из cwd — изолируемся от реального
    client = StubClient()
    stub_runner = StubRunner()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    monkeypatch.setattr(cli_main, "make_agent_runner", lambda c, **kw: stub_runner)
    return CliRunner(), client, stub_runner


def test_agent_command_runs_runner_and_disconnects(agent_cli):
    r, client, stub_runner = agent_cli
    result = r.invoke(cli_main.cli, ["agent"])
    assert result.exit_code == 0
    assert stub_runner.runs == 1
    assert client.connected is False  # disconnect в finally


def test_agent_command_requires_login(agent_cli):
    r, client, stub_runner = agent_cli
    client.authorized = False
    result = r.invoke(cli_main.cli, ["agent"])
    assert result.exit_code != 0
    assert "login" in result.output
    assert stub_runner.runs == 0


def test_agent_command_ctrl_c_says_stopped(agent_cli):
    r, _, stub_runner = agent_cli
    stub_runner.interrupt = True
    result = r.invoke(cli_main.cli, ["agent"])
    assert "stopped." in result.output


def test_agent_listed_in_help(agent_cli):
    r, *_ = agent_cli
    result = r.invoke(cli_main.cli, ["--help"])
    assert "agent" in result.output


def test_missing_extra_gives_pip_hint(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    # import tg_messenger.agent.factory -> ImportError, как при невыставленном extra
    monkeypatch.setitem(sys.modules, "tg_messenger.agent.factory", None)
    result = CliRunner().invoke(cli_main.cli, ["agent"])
    assert result.exit_code != 0
    assert "tg-messenger[agent]" in result.output
    assert "Traceback" not in result.output


def test_bad_config_gives_friendly_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    monkeypatch.delenv("TG_AGENT_MODEL", raising=False)
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["agent"])
    assert result.exit_code != 0
    assert "TG_AGENT_MODEL" in result.output
    assert "Traceback" not in result.output


# --- Цикл 17: статус LangSmith-трассировки при старте агента ---


def test_agent_announces_langsmith_tracing(agent_cli, monkeypatch):
    r, *_ = agent_cli
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "tg-messenger")
    result = r.invoke(cli_main.cli, ["agent"])
    assert result.exit_code == 0
    assert "LangSmith tracing: on (project=tg-messenger)" in result.output


def test_agent_tracing_without_key_fails_fast(agent_cli, monkeypatch):
    r, client, stub_runner = agent_cli
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    result = r.invoke(cli_main.cli, ["agent"])
    assert result.exit_code != 0
    assert "LANGSMITH_API_KEY" in result.output
    assert "Traceback" not in result.output
    assert stub_runner.runs == 0  # до сети и до запуска runner'а


def test_agent_is_silent_about_tracing_when_off(agent_cli, monkeypatch):
    r, *_ = agent_cli
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    result = r.invoke(cli_main.cli, ["agent"])
    assert result.exit_code == 0
    assert "LangSmith" not in result.output
