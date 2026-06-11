"""HTTP client for tg_content_factory (httpx) — the factory is the agent's memory + search.

Auth mechanism (v1, documented): HTTP Basic auth with an EMPTY username and the
shared password (``httpx.BasicAuth("", password)``). It's a mock contract — the
factory side adjusts to match in its companion issue. A 401 surfaces as a clear
``FactoryError`` (never a bare httpx exception).

Resilience: transient network errors (``httpx.TransportError`` — ConnectError,
ReadTimeout, ...) are retried up to ``MAX_ATTEMPTS`` with a simple linear backoff
through an INJECTED ``sleep`` (tests pass a no-op — time never really passes).
HTTP 4xx/5xx responses are NOT network errors: they go straight up as
``FactoryError`` without retries.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5  # seconds * attempt; injected sleep makes this free in tests
PAYLOAD_VERSION = 1


class FactoryError(RuntimeError):
    """Any factory call that failed in a way the caller should see (auth, HTTP, network)."""


class InteropTask(BaseModel):
    """A unit of work the factory hands to the messenger worker (and gets back a result)."""

    id: str
    type: str
    payload: dict[str, Any]
    status: str
    result_payload: dict[str, Any] | None = None


_SleepFn = Callable[[float], Awaitable[None]]


async def _default_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class FactoryClient:
    """Thin async client over the tg_content_factory HTTP API.

    ``http`` is injectable (tests pass an ``AsyncClient`` on a ``MockTransport``);
    when omitted a real ``AsyncClient`` is built from ``base_url``/``password``.
    """

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: _SleepFn | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth("", password)
        self._sleep = sleep or _default_sleep
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=self._base_url)

    async def __aenter__(self) -> FactoryClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # --- low-level request with retry/backoff and error mapping ---

    async def _request(
        self, method: str, path: str, *, params: dict | None = None,
        json: dict | None = None, allow_404_none: bool = False,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._http.request(
                    method, path, params=params, json=json, auth=self._auth,
                )
            except httpx.TransportError as exc:
                # transient: connect/read/network — retry with backoff (injected sleep)
                last_exc = exc
                logger.warning(
                    "factory %s %s network error (attempt %d/%d): %s",
                    method, path, attempt, MAX_ATTEMPTS, exc,
                )
                if attempt < MAX_ATTEMPTS:
                    await self._sleep(BACKOFF_BASE * attempt)
                    continue
                raise FactoryError(
                    f"factory {method} {path} failed after {MAX_ATTEMPTS} attempts: {exc}"
                ) from exc
            return self._handle_response(method, path, response, allow_404_none)
        # unreachable, but keeps the type checker honest
        raise FactoryError(f"factory {method} {path} failed: {last_exc}")

    def _handle_response(
        self, method: str, path: str, response: httpx.Response, allow_404_none: bool
    ) -> Any:
        if allow_404_none and response.status_code == 404:
            return None
        if response.status_code == 401:
            raise FactoryError(
                "factory rejected the request: 401 Unauthorized — check TG_FACTORY_PASSWORD."
            )
        if response.status_code >= 400:
            raise FactoryError(
                f"factory {method} {path} returned HTTP {response.status_code}: {response.text}"
            )
        if not response.content:
            return None
        return response.json()

    # --- cycle 105: search ---

    async def search_messages(
        self, identifier: str, query: str, limit: int = 50,
        date_from: str | None = None, date_to: str | None = None,
        topic_id: int | None = None,
    ) -> list[dict]:
        """Full-text search messages of a chat the factory has indexed.

        ``identifier`` is whatever the factory keys a chat by (@username or id).
        """
        params: dict[str, Any] = {"query": query, "limit": limit}
        if date_from is not None:
            params["date_from"] = date_from
        if date_to is not None:
            params["date_to"] = date_to
        if topic_id is not None:
            params["topic_id"] = topic_id
        result = await self._request("GET", f"/search/messages/{identifier}", params=params)
        return result or []

    # --- cycle 106: tasks ---

    async def create_task(self, type: str, payload: dict) -> str:
        """Enqueue a task; returns its id. ``payload['v']`` is stamped with the version."""
        body = {"type": type, "payload": {**payload, "v": PAYLOAD_VERSION}}
        result = await self._request("POST", "/tasks", json=body)
        return result["id"]

    async def get_task(self, task_id: str) -> dict:
        return await self._request("GET", f"/tasks/{task_id}")

    async def claim_next(self, types: list[str]) -> dict | None:
        """Claim the next pending task of one of ``types``; 404 / empty -> None."""
        params = {"types": ",".join(types)}
        return await self._request("POST", "/tasks/claim", params=params, allow_404_none=True)

    async def complete_task(self, task_id: str, result_payload: dict) -> None:
        await self._request(
            "POST", f"/tasks/{task_id}/complete", json={"result_payload": result_payload}
        )

    async def fail_task(self, task_id: str, error: str) -> None:
        await self._request("POST", f"/tasks/{task_id}/fail", json={"error": error})
