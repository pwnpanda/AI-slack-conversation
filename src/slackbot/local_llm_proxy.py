"""Reverse proxy that mirrors llama.cpp web UI chats into Matrix."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class _MirrorProto(Protocol):
    async def start_web_mirror(
        self,
        room_id: str,
        prompt: str,
        history: list[dict[str, str]],
        conversation_id: str | None,
    ) -> tuple[str, str] | None: ...

    async def update_web_mirror(
        self,
        room_id: str,
        response_event: str,
        answer: str,
    ) -> None: ...

    async def finish_web_mirror(
        self,
        room_id: str,
        thread_root: str,
        response_event: str,
        answer: str,
    ) -> None: ...


class LocalLlmProxy:
    """Proxy llama.cpp and mirror browser-originated chat completions."""

    def __init__(self, backend_url: str, mirror: _MirrorProto, room_id: str) -> None:
        self._backend_url = backend_url.removesuffix("/v1").rstrip("/")
        self._mirror = mirror
        self._room_id = room_id
        self._session: aiohttp.ClientSession | None = None

    def make_app(self) -> web.Application:
        """Create the proxy application."""
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        app.cleanup_ctx.append(self._client_session)
        return app

    async def _client_session(self, _app: web.Application) -> AsyncIterator[None]:
        self._session = aiohttp.ClientSession(auto_decompress=False)
        yield
        await self._session.close()
        self._session = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Forward one request and mirror eligible chat responses."""
        if self._session is None:
            raise RuntimeError("Local model proxy HTTP session is not initialized")
        body = await request.read()
        target = f"{self._backend_url}/{request.match_info['path']}"
        if request.query_string:
            target = f"{target}?{request.query_string}"
        headers = _forward_headers(request.headers)
        async with self._session.request(
            request.method,
            target,
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as backend:
            mirror_input = _mirror_input(request, body)
            if backend.status >= 400 or mirror_input is None:
                return await _copy_response(request, backend)
            history, prompt, conversation_id = mirror_input
            mirror_ids = await self._start_mirror(prompt, history, conversation_id)
            content_type = backend.headers.get("Content-Type", "")
            if content_type.startswith("text/event-stream"):
                return await self._stream_response(request, backend, mirror_ids)
            return await self._buffered_response(request, backend, mirror_ids)

    async def _start_mirror(
        self,
        prompt: str,
        history: list[dict[str, str]],
        conversation_id: str | None,
    ) -> tuple[str, str] | None:
        try:
            return await self._mirror.start_web_mirror(
                self._room_id,
                prompt,
                history,
                conversation_id,
            )
        except Exception:
            log.exception("failed to start web UI Matrix mirror")
            return None

    async def _stream_response(
        self,
        request: web.Request,
        backend: aiohttp.ClientResponse,
        mirror_ids: tuple[str, str] | None,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=backend.status,
            reason=backend.reason,
            headers=_response_headers(backend.headers),
        )
        await response.prepare(request)
        answer = ""
        buffer = ""
        last_edit = 0.0
        async for chunk in backend.content.iter_any():
            await response.write(chunk)
            buffer += chunk.decode("utf-8", errors="replace")
            buffer, additions = _consume_sse(buffer)
            answer += additions
            now = asyncio.get_running_loop().time()
            if mirror_ids is not None and additions and now - last_edit >= 0.75:
                await self._update_mirror(mirror_ids[1], answer)
                last_edit = now
        await response.write_eof()
        if mirror_ids is not None:
            await self._finish_mirror(mirror_ids, answer)
        return response

    async def _buffered_response(
        self,
        request: web.Request,
        backend: aiohttp.ClientResponse,
        mirror_ids: tuple[str, str] | None,
    ) -> web.Response:
        body = await backend.read()
        if mirror_ids is not None:
            answer = _json_answer(body)
            await self._finish_mirror(mirror_ids, answer)
        return web.Response(
            body=body,
            status=backend.status,
            reason=backend.reason,
            headers=_response_headers(backend.headers),
        )

    async def _update_mirror(self, response_event: str, answer: str) -> None:
        try:
            await self._mirror.update_web_mirror(self._room_id, response_event, answer)
        except Exception:
            log.exception("failed to update web UI Matrix mirror")

    async def _finish_mirror(self, mirror_ids: tuple[str, str], answer: str) -> None:
        try:
            await self._mirror.finish_web_mirror(
                self._room_id,
                mirror_ids[0],
                mirror_ids[1],
                answer,
            )
        except Exception:
            log.exception("failed to finish web UI Matrix mirror")


def _forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    forwarded = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() not in ("host", "x-matrix-mirror")
    }
    return forwarded


def _response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


async def _copy_response(
    request: web.Request,
    backend: aiohttp.ClientResponse,
) -> web.StreamResponse:
    content_type = backend.headers.get("Content-Type", "")
    if content_type.startswith("text/event-stream"):
        response = web.StreamResponse(
            status=backend.status,
            reason=backend.reason,
            headers=_response_headers(backend.headers),
        )
        await response.prepare(request)
        async for chunk in backend.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
        return response
    return web.Response(
        body=await backend.read(),
        status=backend.status,
        reason=backend.reason,
        headers=_response_headers(backend.headers),
    )


def _mirror_input(
    request: web.Request,
    body: bytes,
) -> tuple[list[dict[str, str]], str, str | None] | None:
    if request.method != "POST" or request.path.rstrip("/") != "/v1/chat/completions":
        return None
    if not _is_browser_request(request):
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    history = _text_history(messages)
    conversation_id = request.headers.get("X-Conversation-Id") or None
    for message in reversed(history):
        if message["role"] == "user":
            return history, message["content"], conversation_id
    return None


def _is_browser_request(request: web.Request) -> bool:
    if request.headers.get("X-Matrix-Mirror", "").lower() == "true":
        return True
    return any(
        request.headers.get(name)
        for name in ("Origin", "Referer", "Sec-Fetch-Site", "Sec-Fetch-Mode")
    )


def _text_history(messages: list[Any]) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        content = _text_content(message.get("content"))
        if content:
            history.append({"role": message["role"], "content": content})
    return history


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if (
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
    return "\n".join(parts)


def _consume_sse(buffer: str) -> tuple[str, str]:
    answer = ""
    while "\n" in buffer:
        line, buffer = buffer.split("\n", 1)
        line = line.rstrip("\r")
        if not line.startswith("data: "):
            continue
        data = line.removeprefix("data: ")
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
            content = payload["choices"][0]["delta"].get("content")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(content, str):
            answer += content
    return buffer, answer


def _json_answer(body: bytes) -> str:
    try:
        payload = json.loads(body)
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return content if isinstance(content, str) else ""
