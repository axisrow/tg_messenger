"""Agent layer: LangGraph intent orchestrator over the core client.

Depends on ``core`` only; ``core`` never imports this package. Heavy
LLM-stack imports (langchain/langgraph/deepagents) live in ``factory``
and ``orchestrator`` — everything else is stdlib + core.
"""
