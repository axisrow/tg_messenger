"""LangGraph intent orchestrator: route → vision | (classify → chat | task).

No model is called here directly — the LLM contacts are injected
(``classify_fn``, ``chat_fn``, ``task_agent``, ``vision_fn``), which is also
the test seam. Per-dialog memory lives in the checkpointer; ``thread_id`` =
dialog id, so histories never mix. The deep agent is invoked stateless per
turn: it gets the dialog history and only its FINAL answer is appended back —
internal tool/plan messages never leak into the dialog state.

Images never enter the checkpointed state: the history stores a text
placeholder (``IMAGE_PLACEHOLDER`` + caption), and the actual multimodal
message is handed to ``vision_fn`` out of band — so the next text turn of a
non-vision main model never chokes on base64 blocks.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from tg_messenger.agent.config import IntentSpec
from tg_messenger.agent.media import IMAGE_PLACEHOLDER, ImageInput

logger = logging.getLogger(__name__)


def _prefix_instruction(messages: list[BaseMessage], system_prompt: str | None) -> list[BaseMessage]:
    """Подмешать инструкцию интента к последнему user-сообщению — только в payload вызова.

    Компромисс: инструкция уходит user-текстом, не системной ролью — второй
    SystemMessage посреди списка ломается у части провайдеров, а отдельный
    deep-агент на интент неоправданно дорог. Checkpointed-история не меняется.
    """
    if not system_prompt:
        return messages
    last = messages[-1]
    return [*messages[:-1], HumanMessage(content=f"{system_prompt}\n\n{last.content}")]


class TaskAgent(Protocol):
    async def ainvoke(self, payload: dict, config: dict | None = None) -> dict: ...


class OrchestratorState(MessagesState):
    intent: str
    reply: str  # ответ пишется в свой ключ — не полагаемся на порядок в messages
    has_image: bool


class Orchestrator:
    def __init__(
        self,
        *,
        classify_fn: Callable[[str], Awaitable[str]],
        chat_fn: Callable[[list[BaseMessage]], Awaitable[str]],
        task_agent: TaskAgent,
        vision_fn: Callable[[list[BaseMessage]], Awaitable[str]] | None = None,
        intents: Sequence[IntentSpec] = (),
        checkpointer=None,
    ):
        self._classify_fn = classify_fn
        self._chat_fn = chat_fn
        self._task_agent = task_agent
        self._vision_fn = vision_fn
        # multimodal message per thread, handed to the vision node out of band.
        # Valid only because the runner processes events strictly sequentially;
        # if handling ever goes concurrent, move this into per-invoke config.
        self._pending_images: dict[str, HumanMessage] = {}

        graph = StateGraph(OrchestratorState)
        graph.add_node("classify", self._classify)
        graph.add_node("chat", self._chat)
        graph.add_node("task", self._task)
        graph.add_node("vision", self._vision)
        graph.add_conditional_edges(
            START,
            lambda s: "vision" if s.get("has_image") else "classify",
            {"vision": "vision", "classify": "classify"},
        )
        # карта маршрутов строится из конфига: узел на каждый кастомный интент
        routes = {"chat": "chat", "task": "task"}
        for spec in intents:
            graph.add_node(spec.name, self._make_intent_node(spec))
            graph.add_edge(spec.name, END)
            routes[spec.name] = spec.name
        self._routes = frozenset(routes)
        graph.add_conditional_edges("classify", lambda s: s["intent"], routes)
        graph.add_edge("chat", END)
        graph.add_edge("task", END)
        graph.add_edge("vision", END)
        self._graph = graph.compile(checkpointer=checkpointer or InMemorySaver())

    async def _classify(self, state: OrchestratorState) -> dict:
        intent = await self._classify_fn(state["messages"][-1].content)
        if intent not in self._routes:
            # защита от KeyError в conditional edge при произвольном classify_fn
            logger.warning("classifier returned unknown intent %r — falling back to 'chat'", intent)
            intent = "chat"
        return {"intent": intent}

    def _make_intent_node(self, spec: IntentSpec):
        async def intent_node(state: OrchestratorState) -> dict:
            messages = _prefix_instruction(list(state["messages"]), spec.system_prompt)
            if spec.pipeline == "task":
                result = await self._task_agent.ainvoke({"messages": messages})
                reply = result["messages"][-1].content
            else:
                reply = await self._chat_fn(messages)
            return {"messages": [AIMessage(content=reply)], "reply": reply}

        intent_node.__name__ = f"intent_{spec.name}"
        return intent_node

    async def _chat(self, state: OrchestratorState) -> dict:
        reply = await self._chat_fn(list(state["messages"]))
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    async def _task(self, state: OrchestratorState) -> dict:
        result = await self._task_agent.ainvoke({"messages": list(state["messages"])})
        reply = result["messages"][-1].content
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    async def _vision(self, state: OrchestratorState, config: RunnableConfig) -> dict:
        thread_id = config["configurable"]["thread_id"]
        multimodal = self._pending_images.pop(thread_id)
        # история из state (там плейсхолдер последним) + мультимодальное сообщение
        reply = await self._vision_fn([*state["messages"][:-1], multimodal])
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    async def handle(self, dialog_id: int, text: str, *, image: ImageInput | None = None) -> str:
        """Run one incoming message through the graph; returns the reply text."""
        thread_id = str(dialog_id)
        config = {"configurable": {"thread_id": thread_id}}
        if image is None:
            # has_image пишется ЯВНО на каждом ходе — значение не может протухнуть
            payload = {"messages": [HumanMessage(content=text)], "has_image": False}
            result = await self._graph.ainvoke(payload, config)
            return result["reply"]
        if self._vision_fn is None:
            raise RuntimeError(
                "an image arrived but no vision_fn is configured — "
                "build the orchestrator with vision_fn to handle photos"
            )
        placeholder = f"{IMAGE_PLACEHOLDER} {text}".strip() if text else IMAGE_PLACEHOLDER
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.append({"type": "image", "base64": image.base64_data, "mime_type": image.mime_type})
        self._pending_images[thread_id] = HumanMessage(content=blocks)
        try:
            payload = {"messages": [HumanMessage(content=placeholder)], "has_image": True}
            result = await self._graph.ainvoke(payload, config)
        finally:
            self._pending_images.pop(thread_id, None)
        return result["reply"]
