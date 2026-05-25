"""Thin async wrapper over slack_sdk's AsyncWebClient for channel/thread ops."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class SlackIO:
    def __init__(
        self,
        client: Any,
        channel: str,
        agent_channels: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._agent_channels = agent_channels or {}

    def channel_for_agent(self, agent: str) -> str:
        return self._agent_channels.get(agent.lower(), self._channel)

    def _channel_or_default(self, channel: str | None) -> str:
        return channel or self._channel

    async def post_top_level(self, text: str, channel: str | None = None) -> str:
        resp = await self._client.chat_postMessage(
            channel=self._channel_or_default(channel), text=text
        )
        return str(resp["ts"])

    async def post_in_thread(self, thread_ts: str, text: str, channel: str | None = None) -> str:
        resp = await self._client.chat_postMessage(
            channel=self._channel_or_default(channel), text=text, thread_ts=thread_ts
        )
        return str(resp["ts"])

    async def edit_top_level(self, ts: str, text: str, channel: str | None = None) -> None:
        await self._client.chat_update(channel=self._channel_or_default(channel), ts=ts, text=text)

    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None:
        # Reactions are cosmetic confirmation; never let them fail a delivery.
        try:
            await self._client.reactions_add(
                channel=self._channel_or_default(channel), timestamp=ts, name=emoji
            )
        except Exception as exc:
            log.warning("reaction %s on %s failed (non-fatal): %s", emoji, ts, exc)
