import json
from dataclasses import dataclass, field

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from slackbot.local_llm_proxy import LocalLlmProxy


@dataclass
class FakeMirror:
    starts: list[tuple[str, str, list[dict[str, str]], str | None]] = field(default_factory=list)
    updates: list[tuple[str, str, str]] = field(default_factory=list)
    finishes: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def start_web_mirror(
        self,
        room_id: str,
        prompt: str,
        history: list[dict[str, str]],
        conversation_id: str | None,
    ) -> tuple[str, str]:
        self.starts.append((room_id, prompt, history, conversation_id))
        return "$root", "$response"

    async def update_web_mirror(
        self,
        room_id: str,
        response_event: str,
        answer: str,
    ) -> None:
        self.updates.append((room_id, response_event, answer))

    async def finish_web_mirror(
        self,
        room_id: str,
        thread_root: str,
        response_event: str,
        answer: str,
    ) -> None:
        self.finishes.append((room_id, thread_root, response_event, answer))


async def _proxy_client(aiohttp_server, mirror: FakeMirror) -> TestClient:
    async def completion(request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == "Bearer secret"
        assert "X-Matrix-Mirror" not in request.headers
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for content in ("one", " two"):
            data = {"choices": [{"delta": {"content": content}}]}
            await response.write(f"data: {json.dumps(data)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text="llama UI")

    backend = web.Application()
    backend.router.add_post("/v1/chat/completions", completion)
    backend.router.add_get("/", index)
    backend_server = await aiohttp_server(backend)
    proxy = LocalLlmProxy(str(backend_server.make_url("/v1")), mirror, "!room")
    server = TestServer(proxy.make_app())
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_browser_stream_is_forwarded_and_mirrored(aiohttp_server) -> None:
    mirror = FakeMirror()
    client = await _proxy_client(aiohttp_server, mirror)
    payload = {
        "messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "new"},
        ],
        "stream": True,
    }
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer secret",
                "Sec-Fetch-Site": "same-origin",
                "X-Conversation-Id": "browser-conversation",
            },
            json=payload,
        )
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert "data: [DONE]" in body
    assert mirror.starts == [("!room", "new", payload["messages"], "browser-conversation")]
    assert mirror.finishes == [("!room", "$root", "$response", "one two")]


@pytest.mark.asyncio
async def test_api_request_without_browser_headers_is_not_mirrored(aiohttp_server) -> None:
    mirror = FakeMirror()
    client = await _proxy_client(aiohttp_server, mirror)
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={"messages": [{"role": "user", "content": "private"}]},
        )
        await response.read()
    finally:
        await client.close()

    assert response.status == 200
    assert mirror.starts == []


@pytest.mark.asyncio
async def test_non_object_browser_payload_is_forwarded_without_mirroring(aiohttp_server) -> None:
    mirror = FakeMirror()
    client = await _proxy_client(aiohttp_server, mirror)
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer secret",
                "Sec-Fetch-Site": "same-origin",
            },
            json=[],
        )
        await response.read()
    finally:
        await client.close()

    assert response.status == 200
    assert mirror.starts == []


@pytest.mark.asyncio
async def test_web_ui_assets_are_proxied(aiohttp_server) -> None:
    mirror = FakeMirror()
    client = await _proxy_client(aiohttp_server, mirror)
    try:
        response = await client.get("/")
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert body == "llama UI"
