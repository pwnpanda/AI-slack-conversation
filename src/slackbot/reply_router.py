"""Routes Slack thread replies to the originating Zellij pane."""

from __future__ import annotations

import logging
from typing import Protocol

from slackbot.dedupe import DeliveryDedupe
from slackbot.registry import Registry
from slackbot.zellij_io import ZellijError

log = logging.getLogger(__name__)


class _ActuatorProto(Protocol):
    async def deliver(self, session: str, pane_id: str, text: str) -> None: ...


class _SlackIOProto(Protocol):
    async def post_in_thread(
        self, thread_ts: str, text: str, channel: str | None = None
    ) -> str: ...
    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None: ...


class ReplyRouter:
    def __init__(
        self,
        reg: Registry,
        actuator: _ActuatorProto,
        slack: _SlackIOProto,
        dedupe: DeliveryDedupe | None = None,
    ) -> None:
        self._reg = reg
        self._actuator = actuator
        self._slack = slack
        self._dedupe = dedupe

    async def on_reply(self, channel: str, thread_ts: str, text: str, msg_ts: str) -> None:
        sess = self._reg.get_session_by_thread(thread_ts, channel)
        if sess is None:
            log.debug("reply for unknown thread %s ignored", thread_ts)
            return

        if sess.status == "ended":
            await self._slack.post_in_thread(
                thread_ts, "⚠️ session offline, reply not sent", channel=channel
            )
            await self._slack.react(msg_ts, "no_entry_sign", channel=channel)
            return

        if not sess.zellij_session or not sess.zellij_pane_id:
            await self._slack.post_in_thread(
                thread_ts, "❌ delivery failed: session has no pane info", channel=channel
            )
            await self._slack.react(msg_ts, "warning", channel=channel)
            return

        try:
            await self._actuator.deliver(sess.zellij_session, sess.zellij_pane_id, text)
        except ZellijError as exc:
            await self._slack.post_in_thread(
                thread_ts, f"❌ delivery failed: {exc}", channel=channel
            )
            await self._slack.react(msg_ts, "warning", channel=channel)
            return

        # Suppress the echo: the UserPromptSubmit hook will fire with this text
        # once CC accepts the prompt. We tell handlers to skip mirroring it.
        if self._dedupe:
            self._dedupe.mark(sess.cc_session_id, text)
        await self._slack.react(msg_ts, "white_check_mark", channel=channel)
