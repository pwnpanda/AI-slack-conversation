"""Slack-side slash-style commands typed as top-level channel messages.

Currently supports `/new <name>` which reserves a Slack thread for a name
that a CC session can later bind to via `/rn <name>`.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from slackbot.events import top_level_text
from slackbot.registry import Registry

log = logging.getLogger(__name__)

_NEW_PATTERN = re.compile(r"^/new\s+(\S+)\s*$")


class _SlackIOProto(Protocol):
    async def post_top_level(self, text: str, channel: str | None = None) -> str: ...
    async def post_in_thread(
        self, thread_ts: str, text: str, channel: str | None = None
    ) -> str: ...
    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None: ...


class SlackCommandHandler:
    """Dispatch top-level Slack messages that start with a slash command."""

    def __init__(self, reg: Registry, slack: _SlackIOProto) -> None:
        self._reg = reg
        self._slack = slack

    async def maybe_handle(self, channel: str, text: str, msg_ts: str) -> bool:
        """Return True if *text* matched a command and was handled."""
        m = _NEW_PATTERN.match(text.strip())
        if not m:
            return False
        await self._handle_new(channel=channel, name=m.group(1), msg_ts=msg_ts)
        return True

    async def _handle_new(self, channel: str, name: str, msg_ts: str) -> None:
        existing = self._reg.get_session_by_name(name, channel=channel)
        if existing is not None:
            await self._slack.react(msg_ts, "x", channel=channel)
            await self._slack.post_in_thread(
                msg_ts,
                f"❌ Name `{name}` already in use by session "
                f"`{existing.cc_session_id}` (status: {existing.status}). "
                f"Pick a different name or rename the existing one.",
                channel=channel,
            )
            return
        thread_ts = await self._slack.post_top_level(
            top_level_text(name, "(reserved)", "active", "claude"),
            channel=channel,
        )
        self._reg.reserve_name(name, channel, thread_ts)
        await self._slack.post_in_thread(
            thread_ts,
            f"_Reserved by `/new {name}`. "
            "Bind a CC session by typing `/rn {name}` inside Claude Code._".replace("{name}", name),
            channel=channel,
        )
        await self._slack.react(msg_ts, "white_check_mark", channel=channel)
