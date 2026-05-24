"""aiohttp HTTP server: receives CC hook events on POST /event."""

from __future__ import annotations

import json
import logging
from typing import Protocol

from aiohttp import web

log = logging.getLogger(__name__)


class _HandlersProto(Protocol):
    async def handle(self, event: dict) -> None: ...


def make_app(handlers: _HandlersProto) -> web.Application:
    app = web.Application()

    async def post_event(request: web.Request) -> web.Response:
        try:
            body = await request.text()
            event = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return web.Response(status=400, text="invalid json")
        try:
            await handlers.handle(event)
        except Exception:
            log.exception("handler error for event=%r", event)
            return web.Response(status=500, text="handler error")
        return web.Response(status=204)

    async def healthz(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_post("/event", post_event)
    app.router.add_get("/healthz", healthz)
    return app
