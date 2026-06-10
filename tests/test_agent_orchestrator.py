"""Цикл 12: Orchestrator — LangGraph-граф classify → chat | task.

LLM-контакты — инжектируемые фейки (async-функции и стаб deep-агента),
сам граф, память и маршрутизация — настоящие langgraph.
"""

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402

from tg_messenger.agent.orchestrator import Orchestrator  # noqa: E402


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


def build(intent="chat", **kw):
    classify, classify_calls = make_classify(intent)
    chat, chat_calls = make_chat(kw.pop("chat_reply", "chat reply"))
    agent = StubDeepAgent(kw.pop("task_reply", "task done"))
    orch = Orchestrator(classify_fn=classify, chat_fn=chat, task_agent=agent)
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
