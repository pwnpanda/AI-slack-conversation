"""Thin async wrapper over slack_sdk's AsyncWebClient for channel/thread ops."""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class _AsyncSlackClient(Protocol):
    async def chat_postMessage(self, **kwargs: object) -> dict: ...
    async def chat_update(self, **kwargs: object) -> dict: ...
    async def reactions_add(self, **kwargs: object) -> dict: ...


class SlackIO:
    def __init__(self, client: _AsyncSlackClient, channel: str) -> None:
        self._client = client
        self._channel = channel

    async def post_top_level(self, text: str) -> str:
        resp = await self._client.chat_postMessage(channel=self._channel, text=text)
        return str(resp["ts"])

    async def post_in_thread(self, thread_ts: str, text: str) -> str:
        resp = await self._client.chat_postMessage(
            channel=self._channel, text=text, thread_ts=thread_ts
        )
        return str(resp["ts"])

    async def edit_top_level(self, ts: str, text: str) -> None:
        await self._client.chat_update(channel=self._channel, ts=ts, text=text)

    async def react(self, ts: str, emoji: str) -> None:
        # Reactions are cosmetic confirmation; never let them fail a delivery.
        try:
            await self._client.reactions_add(channel=self._channel, timestamp=ts, name=emoji)
        except Exception as exc:
            log.warning("reaction %s on %s failed (non-fatal): %s", emoji, ts, exc)
