import json

import pytest
from aiohttp import web

from slackbot.local_llm_client import LocalLlamaClient


@pytest.mark.asyncio
async def test_stream_chat_reads_openai_sse(aiohttp_server) -> None:
    async def completion(request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == "Bearer secret"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for content in ("one", " two"):
            data = {"choices": [{"delta": {"content": content}}]}
            await response.write(f"data: {json.dumps(data)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completion)
    server = await aiohttp_server(app)
    client = LocalLlamaClient(str(server.make_url("/v1")), "secret", "local")

    chunks = [chunk async for chunk in client.stream_chat([{"role": "user", "content": "x"}])]

    assert chunks == ["one", " two"]


@pytest.mark.asyncio
async def test_is_sleeping_uses_server_props(aiohttp_server) -> None:
    async def props(_: web.Request) -> web.Response:
        return web.json_response({"is_sleeping": True})

    app = web.Application()
    app.router.add_get("/props", props)
    server = await aiohttp_server(app)
    client = LocalLlamaClient(str(server.make_url("/v1")), "secret", "local")

    assert await client.is_sleeping() is True
