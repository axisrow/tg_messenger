"""LangGraph intent orchestrator: classify → chat | task.

No model is called here directly — the three LLM contacts are injected
(``classify_fn``, ``chat_fn``, ``task_agent``), which is also the test seam.
Per-dialog memory lives in the checkpointer; ``thread_id`` = dialog id, so
histories never mix. The deep agent is invoked stateless per turn: it gets
the dialog history and only its FINAL answer is appended back — internal
tool/plan messages never leak into the dialog state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

Intent = Literal["chat", "task"]


class TaskAgent(Protocol):
    async def ainvoke(self, payload: dict, config: dict | None = None) -> dict: ...


class OrchestratorState(MessagesState):
    intent: Intent
    reply: str  # ответ пишется в свой ключ — не полагаемся на порядок в messages


class Orchestrator:
    def __init__(
        self,
        *,
        classify_fn: Callable[[str], Awaitable[Intent]],
        chat_fn: Callable[[list[BaseMessage]], Awaitable[str]],
        task_agent: TaskAgent,
        checkpointer=None,
    ):
        self._classify_fn = classify_fn
        self._chat_fn = chat_fn
        self._task_agent = task_agent

        graph = StateGraph(OrchestratorState)
        graph.add_node("classify", self._classify)
        graph.add_node("chat", self._chat)
        graph.add_node("task", self._task)
        graph.add_edge(START, "classify")
        graph.add_conditional_edges("classify", lambda s: s["intent"], {"chat": "chat", "task": "task"})
        graph.add_edge("chat", END)
        graph.add_edge("task", END)
        self._graph = graph.compile(checkpointer=checkpointer or InMemorySaver())

    async def _classify(self, state: OrchestratorState) -> dict:
        intent = await self._classify_fn(state["messages"][-1].content)
        return {"intent": intent}

    async def _chat(self, state: OrchestratorState) -> dict:
        reply = await self._chat_fn(list(state["messages"]))
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    async def _task(self, state: OrchestratorState) -> dict:
        result = await self._task_agent.ainvoke({"messages": list(state["messages"])})
        reply = result["messages"][-1].content
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    async def handle(self, dialog_id: int, text: str) -> str:
        """Run one incoming message through the graph; returns the reply text."""
        config = {"configurable": {"thread_id": str(dialog_id)}}
        result = await self._graph.ainvoke({"messages": [HumanMessage(content=text)]}, config)
        return result["reply"]
