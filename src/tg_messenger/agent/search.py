"""Web-search tool factory — one provider per build, imported lazily.

Only the chosen provider's SDK must be installed; a missing package or
API key fails fast at build time (process start), not on the first query.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

from tg_messenger.agent.config import SEARCH_PROVIDERS

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


def _format_results(rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return "No results found."
    lines = []
    for i, (title, url, snippet) in enumerate(rows, start=1):
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n".join(lines)


def _require_key(name: str, provider: str) -> str:
    key = os.environ.get(name, "").strip()
    if not key:
        raise ValueError(f"{name} is not set — the '{provider}' search provider requires it.")
    return key


def _require_import(provider: str, pip_name: str, importer: Callable):
    try:
        return importer()
    except ImportError as exc:
        raise ValueError(
            f"Search provider '{provider}' needs an extra package: pip install {pip_name}"
        ) from exc


def build_search_fn(provider: str) -> Callable[..., Awaitable[str]]:
    """Return an async ``web_search(query, max_results=5) -> str`` for the provider."""
    if provider == "duckduckgo":

        def _import():
            from ddgs import DDGS
            return DDGS

        ddgs_cls = _require_import(provider, "ddgs", _import)

        async def _search(query: str, max_results: int) -> str:
            def _call():
                # list() здесь же: ddgs может вернуть ленивый генератор, его I/O
                # должен исчерпаться в этом потоке, а не в event loop
                return list(ddgs_cls().text(query, max_results=max_results))

            rows = await asyncio.to_thread(_call)  # синхронный SDK
            return _format_results([(r["title"], r["href"], r["body"]) for r in rows])

    elif provider == "tavily":

        def _import():
            from tavily import TavilyClient
            return TavilyClient

        tavily_cls = _require_import(provider, "tavily-python", _import)
        tavily = tavily_cls(api_key=_require_key("TAVILY_API_KEY", provider))

        async def _search(query: str, max_results: int) -> str:
            data = await asyncio.to_thread(tavily.search, query, max_results=max_results)
            rows = [(r["title"], r["url"], r["content"]) for r in data.get("results", [])]
            return _format_results(rows)

    elif provider == "exa":

        def _import():
            from exa_py import Exa
            return Exa

        exa_cls = _require_import(provider, "exa-py", _import)
        exa = exa_cls(api_key=_require_key("EXA_API_KEY", provider))

        async def _search(query: str, max_results: int) -> str:
            def _call():
                return exa.search_and_contents(query, num_results=max_results, text=True)

            data = await asyncio.to_thread(_call)
            return _format_results([(r.title, r.url, r.text) for r in data.results])

    elif provider == "serpdive":

        def _import():
            from serpdive import SerpDive
            return SerpDive

        serpdive_cls = _require_import(provider, "serpdive", _import)
        serpdive = serpdive_cls(api_key=_require_key("SERPDIVE_API_KEY", provider))

        async def _search(query: str, max_results: int) -> str:
            data = await asyncio.to_thread(serpdive.search, query, max_results=max_results)
            # content is the extracted text of the page; title can be absent
            rows = [(r.title or r.url, r.url, r.content) for r in data.results]
            return _format_results(rows)

    elif provider == "brave":
        key = _require_key("BRAVE_API_KEY", provider)

        async def _search(query: str, max_results: int) -> str:
            import httpx

            headers = {"X-Subscription-Token": key, "Accept": "application/json"}
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get(
                    BRAVE_SEARCH_URL, params={"q": query, "count": max_results}, headers=headers
                )
                resp.raise_for_status()
                data = resp.json()
            rows = [
                (r["title"], r["url"], r.get("description", ""))
                for r in data.get("web", {}).get("results", [])
            ]
            return _format_results(rows)

    else:
        raise ValueError(
            f"Search provider {provider!r} is unknown — choose one of: {', '.join(SEARCH_PROVIDERS)}."
        )

    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the web and return numbered results (title, URL, snippet).

        Args:
            query: Search query.
            max_results: Max number of results to return.
        """
        return await _search(query, max_results)

    return web_search
