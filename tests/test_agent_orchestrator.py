"""Цикл 12: Orchestrator — LangGraph-граф classify → chat | task.

LLM-контакты — инжектируемые фейки (async-функции и стаб deep-агента),
сам граф, память и маршрутизация — настоящие langgraph.
"""

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402

from tg_messenger.agent.config import IntentSpec  # noqa: E402
from tg_messenger.agent.media import IMAGE_PLACEHOLDER, ImageInput  # noqa: E402
from tg_messenger.agent.orchestrator import Orchestrator  # noqa: E402

IMG = ImageInput(base64_data="QUJD", mime_type="image/png")  # b"ABC"


def make_classify(intent):
    calls = []

    async def classify(text: str) -> str:
        calls.append(text)
        return intent

    return classify, calls


def make_chat(reply="chat reply"):
    calls = []

    async def chat(messages) -> str:
        calls.append(list(messages))
        return reply

    return chat, calls


class StubDeepAgent:
    def __init__(self, reply="task done"):
        self.calls = []
        self.reply = reply

    async def ainvoke(self, payload, config=None):
        self.calls.append(payload)
        # как настоящий deep-агент: внутри полно служебных сообщений
        return {"messages": [*payload["messages"], AIMessage(content="thinking..."),
                             AIMessage(content=self.reply)]}


def make_vision(reply="vision reply"):
    calls = []

    async def vision(messages) -> str:
        calls.append(list(messages))
        return reply

    return vision, calls


def build(intent="chat", **kw):
    classify, classify_calls = make_classify(intent)
    chat, chat_calls = make_chat(kw.pop("chat_reply", "chat reply"))
    agent = StubDeepAgent(kw.pop("task_reply", "task done"))
    orch = Orchestrator(classify_fn=classify, chat_fn=chat, task_agent=agent, **kw)
    return orch, classify_calls, chat_calls, agent


async def test_chat_intent_routes_to_chat_fn_only():
    orch, classify_calls, chat_calls, agent = build(intent="chat")
    reply = await orch.handle(7, "привет")
    assert reply == "chat reply"
    assert classify_calls == ["привет"]  # классификатор видит именно текст сообщения
    assert agent.calls == []  # deep-агент не тронут
    assert chat_calls[0][-1].content == "привет"


async def test_task_intent_routes_to_deep_agent():
    orch, _, chat_calls, agent = build(intent="task")
    reply = await orch.handle(7, "найди и отправь")
    assert reply == "task done"
    assert chat_calls == []
    (payload,) = agent.calls
    assert payload["messages"][-1].content == "найди и отправь"


async def test_dialog_memory_persists_between_turns():
    orch, _, chat_calls, _ = build(intent="chat")
    await orch.handle(7, "первое")
    await orch.handle(7, "второе")
    seen = [m.content for m in chat_calls[1]]
    assert seen == ["первое", "chat reply", "второе"]


async def test_dialogs_are_isolated_by_thread_id():
    orch, _, chat_calls, _ = build(intent="chat")
    await orch.handle(7, "секрет диалога 7")
    await orch.handle(8, "привет из 8")
    seen = [m.content for m in chat_calls[1]]
    assert seen == ["привет из 8"]  # истории диалога 7 здесь нет


async def test_deep_agent_internals_do_not_leak_into_history():
    intents = ["task", "chat"]

    async def classify(text: str) -> str:
        return intents.pop(0)

    chat, chat_calls = make_chat()
    orch = Orchestrator(classify_fn=classify, chat_fn=chat, task_agent=StubDeepAgent("task done"))
    await orch.handle(7, "задание")
    # второй ход — chat, смотрим, что накопилось в истории диалога
    await orch.handle(7, "а теперь поболтаем")
    seen = [m.content for m in chat_calls[0]]
    # только финальный ответ deep-агента, без "thinking..."
    assert seen == ["задание", "task done", "а теперь поболтаем"]


# --- Цикл 21: vision-узел ---


async def test_image_routes_to_vision_only():
    vision, vision_calls = make_vision()
    orch, classify_calls, chat_calls, agent = build(vision_fn=vision)
    reply = await orch.handle(7, "что тут?", image=IMG)
    assert reply == "vision reply"
    assert classify_calls == [] and chat_calls == [] and agent.calls == []
    (messages,) = vision_calls
    blocks = messages[-1].content  # последнее сообщение — мультимодальное
    assert {"type": "text", "text": "что тут?"} in blocks
    image_block = next(b for b in blocks if b["type"] == "image")
    assert image_block["base64"] == "QUJD"
    assert image_block["mime_type"] == "image/png"


async def test_image_without_caption_sends_only_image_block():
    vision, vision_calls = make_vision()
    orch, *_ = build(vision_fn=vision)
    await orch.handle(7, "", image=IMG)
    blocks = vision_calls[0][-1].content
    assert [b["type"] for b in blocks] == ["image"]  # пустой text-блок не шлём


async def test_vision_sees_dialog_history():
    vision, vision_calls = make_vision()
    orch, *_ = build(intent="chat", vision_fn=vision)
    await orch.handle(7, "привет")
    await orch.handle(7, "а это что?", image=IMG)
    seen = [m.content for m in vision_calls[0][:-1]]
    assert seen == ["привет", "chat reply"]


async def test_history_keeps_placeholder_not_base64():
    vision, _ = make_vision()
    orch, _, chat_calls, _ = build(intent="chat", vision_fn=vision)
    await orch.handle(7, "что тут?", image=IMG)
    await orch.handle(7, "поболтаем")
    seen = [m.content for m in chat_calls[0]]
    # в истории — текстовый плейсхолдер и ответ vision, никакого base64
    assert seen == [f"{IMAGE_PLACEHOLDER} что тут?", "vision reply", "поболтаем"]
    assert not any("QUJD" in str(c) for c in seen)


async def test_image_turn_does_not_poison_next_text_turn():
    # has_image не «протухает»: следующий текстовый ход идёт через classify
    vision, vision_calls = make_vision()
    orch, classify_calls, chat_calls, _ = build(intent="chat", vision_fn=vision)
    await orch.handle(7, "", image=IMG)
    await orch.handle(7, "обычный текст")
    assert classify_calls == ["обычный текст"]
    assert len(vision_calls) == 1


async def test_image_without_caption_stores_bare_placeholder():
    vision, _ = make_vision()
    orch, _, chat_calls, _ = build(intent="chat", vision_fn=vision)
    await orch.handle(7, "", image=IMG)
    await orch.handle(7, "дальше")
    assert chat_calls[0][0].content == IMAGE_PLACEHOLDER


async def test_image_without_vision_fn_is_a_hard_error():
    orch, classify_calls, *_ = build()
    with pytest.raises(RuntimeError):
        await orch.handle(7, "что тут?", image=IMG)
    assert classify_calls == []  # упали до графа, история не тронута


# --- Цикл 24: кастомные интент-узлы из конфига ---

RECIPE = IntentSpec(name="recipe", description="просит рецепт", pipeline="chat",
                    system_prompt="Ты повар.")
RESEARCH = IntentSpec(name="research", description="просит исследование", pipeline="task",
                      system_prompt="Копай глубоко.")
PLAIN = IntentSpec(name="plain", description="что-то простое", pipeline="chat")


async def test_custom_chat_intent_prefixes_instruction():
    orch, _, chat_calls, agent = build(intent="recipe", intents=(RECIPE,))
    reply = await orch.handle(7, "борщ")
    assert reply == "chat reply"
    assert agent.calls == []
    assert chat_calls[0][-1].content == "Ты повар.\n\nборщ"


async def test_custom_intent_history_keeps_original_text():
    intents = ["recipe", "chat"]

    async def classify(text: str) -> str:
        return intents.pop(0)

    chat, chat_calls = make_chat()
    orch = Orchestrator(classify_fn=classify, chat_fn=chat,
                        task_agent=StubDeepAgent(), intents=(RECIPE,))
    await orch.handle(7, "борщ")
    await orch.handle(7, "поболтаем")
    seen = [m.content for m in chat_calls[1]]
    # в checkpointed-истории — оригинальный текст, без инструкции интента
    assert seen == ["борщ", "chat reply", "поболтаем"]


async def test_custom_task_intent_prefixes_deep_agent_payload():
    orch, _, chat_calls, agent = build(intent="research", intents=(RESEARCH,))
    reply = await orch.handle(7, "изучи тему")
    assert reply == "task done"
    assert chat_calls == []
    (payload,) = agent.calls
    assert payload["messages"][-1].content == "Копай глубоко.\n\nизучи тему"


async def test_custom_intent_without_prompt_passes_message_unchanged():
    orch, _, chat_calls, _ = build(intent="plain", intents=(PLAIN,))
    await orch.handle(7, "вопрос")
    assert chat_calls[0][-1].content == "вопрос"


async def test_unknown_intent_from_classifier_falls_back_to_chat(caplog):
    import logging

    orch, _, chat_calls, agent = build(intent="garbage", intents=(RECIPE,))
    with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.orchestrator"):
        reply = await orch.handle(7, "что-нибудь")
    assert reply == "chat reply"  # не KeyError в conditional edge
    assert agent.calls == []
    assert any("garbage" in r.message for r in caplog.records)  # не молча
