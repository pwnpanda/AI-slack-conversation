from dataclasses import dataclass, field

import pytest

from slackbot.server import make_app


@dataclass
class FakeHandlers:
    received: list[dict] = field(default_factory=list)
    raise_exc: Exception | None = None

    async def handle(self, event: dict) -> None:
        if self.raise_exc:
            raise self.raise_exc
        self.received.append(event)


@pytest.mark.asyncio
async def test_post_event_dispatches_to_handlers(aiohttp_client) -> None:
    handlers = FakeHandlers()
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/event",
        json={"v": 1, "kind": "prompt", "session_id": "s1", "text": "hi"},
    )
    assert resp.status == 204
    assert handlers.received == [{"v": 1, "kind": "prompt", "session_id": "s1", "text": "hi"}]


@pytest.mark.asyncio
async def test_post_event_returns_400_on_invalid_json(aiohttp_client) -> None:
    handlers = FakeHandlers()
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.post("/event", data="not-json")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_post_event_returns_500_on_handler_error(aiohttp_client) -> None:
    handlers = FakeHandlers(raise_exc=RuntimeError("boom"))
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.post("/event", json={"v": 1, "kind": "x", "session_id": "s"})
    assert resp.status == 500


@pytest.mark.asyncio
async def test_get_healthz(aiohttp_client) -> None:
    handlers = FakeHandlers()
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.get("/healthz")
    assert resp.status == 200
