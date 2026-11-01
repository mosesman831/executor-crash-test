from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import SecretStr

from crashtest.sse import iter_sse, parse_event

BASE = "https://api.openai.com/v1"
BETA = {"OpenAI-Beta": "agents=v1"}


class AgentsAPIError(Exception):
    def __init__(self, status: int, code: str | None, body: Any) -> None:
        super().__init__(f"agents api {status} {code}")
        self.status = status
        self.code = code
        self.body = body


class AgentsClient:
    def __init__(self, api_key: SecretStr, *, timeout: float = 60.0) -> None:
        self._key = api_key
        self._timeout = timeout
        self._client = httpx.AsyncClient(base_url=BASE, timeout=timeout)
        self.stream_query_used: bool | None = None

    def _headers(self, extra: dict[str, str] | None = None, *, beta: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._key.get_secret_value()}",
            **(BETA if beta else {}),
        }
        if extra:
            headers.update(extra)
        return headers

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retry: bool = True,
        beta: bool = True,
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers=self._headers(headers, beta=beta),
                    timeout=timeout or self._timeout,
                )
            except httpx.TransportError:
                if not retry or attempt >= 4:
                    raise
                await asyncio.sleep(_backoff(attempt, None))
                continue
            if response.status_code in {429, 500, 503} and retry and attempt < 4:
                await asyncio.sleep(_backoff(attempt, response.headers.get("Retry-After")))
                continue
            if response.status_code >= 400:
                code, body = _error_parts(response)
                raise AgentsAPIError(response.status_code, code, body)
            return response

    async def create_session(
        self,
        *,
        model: str,
        instructions: str,
        workspace_directory: str,
        metadata: dict[str, str],
    ) -> dict:
        response = await self._request(
            "POST",
            "/agents/sessions",
            json={
                "agent": {"model": model, "instructions": instructions},
                "environment": {
                    "type": "self_hosted",
                    "workspace_directory": workspace_directory,
                },
                "metadata": metadata,
            },
            retry=False,
        )
        return response.json()

    async def retrieve_session(self, session_id: str) -> dict:
        response = await self._request("GET", f"/agents/sessions/{session_id}")
        return response.json()

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/agents/sessions/{session_id}", retry=False)

    async def send_message(self, session_id: str, text: str, idempotency_key: str) -> int:
        response = await self._request(
            "POST",
            f"/agents/sessions/{session_id}/events",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "events": [
                    {
                        "type": "agent.session.input.message",
                        "input": [
                            {
                                "role": "user",
                                "type": "message",
                                "content": [{"type": "input_text", "text": text}],
                            }
                        ],
                    }
                ]
            },
            timeout=330.0,
        )
        return response.status_code

    async def cancel(self, session_id: str, idempotency_key: str) -> int:
        response = await self._request(
            "POST",
            f"/agents/sessions/{session_id}/events",
            headers={"Idempotency-Key": idempotency_key},
            json={"events": [{"type": "agent.session.input.cancel"}]},
            timeout=60.0,
        )
        return response.status_code

    async def list_items(self, session_id: str) -> list[dict]:
        return await self._pages(f"/agents/sessions/{session_id}/items", {"order": "asc", "limit": 100})

    async def list_turns(self, session_id: str) -> list[dict]:
        return await self._pages(
            f"/agents/sessions/{session_id}/turns",
            {"order": "desc", "limit": 20},
        )

    async def retrieve_turn(self, session_id: str, turn_id: str) -> dict:
        response = await self._request(
            "GET", f"/agents/sessions/{session_id}/turns/{turn_id}"
        )
        return response.json()

    async def _pages(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        query = dict(params)
        cursors: set[str] = set()
        while True:
            response = await self._request("GET", path, params=query)
            body = response.json()
            items.extend(body.get("data") or [])
            if not body.get("has_more"):
                return items
            last_id = body.get("last_id")
            if not isinstance(last_id, str) or not last_id.strip() or last_id in cursors:
                raise ValueError("pagination response has no advancing cursor")
            cursors.add(last_id)
            query["after"] = last_id

    async def create_decision(self, request: dict) -> dict:
        response = await self._request(
            "POST", "/decisions", json=request,
            headers={"Content-Type": "application/json"}, timeout=10.0,
            retry=False, beta=False,
        )
        return response.json()

    async def stream_events(self, session_id: str, *, ready: asyncio.Event | None = None) -> AsyncIterator[dict]:
        path = f"/agents/sessions/{session_id}/events"
        headers = self._headers({"Accept": "text/event-stream"})
        self.stream_query_used = None
        for use_query in (True, False):
            params = {"stream": "true"} if use_query else None
            async with self._client.stream(
                "GET", path, params=params, headers=headers, timeout=None
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    if response.status_code == 400 and use_query:
                        continue
                    code, body = _error_parts(response)
                    raise AgentsAPIError(response.status_code, code, body)
                self.stream_query_used = use_query
                if ready is not None:
                    ready.set()
                async for msg in iter_sse(response.aiter_lines()):
                    event = parse_event(msg)
                    if event is not None:
                        yield event
                return

    async def probe_access(self) -> str:
        try:
            await self._request("GET", "/agents/sessions", params={"limit": 1}, retry=False)
        except AgentsAPIError as exc:
            if exc.status in {401, 403, 404}:
                return "no_access"
            return "unknown"
        except httpx.TransportError:
            return "unknown"
        return "ok"


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(8.0, (2 ** (attempt - 1)) + random.random())


def _error_parts(response: httpx.Response) -> tuple[str | None, Any]:
    try:
        body = response.json()
    except Exception:
        return None, response.text[:500]
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code, body
