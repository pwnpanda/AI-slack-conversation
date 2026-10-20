"""Streaming client for a local llama.cpp server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import aiohttp


class LocalLlamaClient:
    """Call llama.cpp through its OpenAI-compatible HTTP API."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)

    async def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "temperature": 0.2,
            "max_tokens": 1024,
        }
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as session,
            session.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response,
        ):
            await self._raise_for_status(response, "stream local chat")
            async for raw_line in response.content:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ")
                if data == "[DONE]":
                    return
                chunk = json.loads(data)
                content = chunk["choices"][0]["delta"].get("content")
                if content:
                    yield str(content)

    async def keep_alive(self) -> None:
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": "Reply OK."}],
            "stream": False,
            "temperature": 0,
            "max_tokens": 1,
        }
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as session,
            session.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response,
        ):
            await self._raise_for_status(response, "keep local model loaded")

    async def is_sleeping(self) -> bool:
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as session,
            session.get(
                f"{self._base_url.removesuffix('/v1')}/props",
                headers=self._headers(),
            ) as response,
        ):
            await self._raise_for_status(response, "read local model state")
            payload = await response.json()
        return bool(payload.get("is_sleeping"))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    async def _raise_for_status(response: aiohttp.ClientResponse, operation: str) -> None:
        if response.status < 400:
            return
        body = (await response.text())[:500]
        raise RuntimeError(f"Failed to {operation}: HTTP {response.status}: {body}")
