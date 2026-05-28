"""Web-API fallback for delivering Slack thread replies.

Socket Mode occasionally zombies silently — the connection looks healthy
(is_connected=True, ping/pong flowing) but message events stop reaching
on_message. The slack-sdk has no API to detect this, so we add a
periodic poll of conversations.replies for each tracked thread.

If Socket Mode is healthy, this poller has nothing to do — every message
has already been processed by Bolt's on_message and recorded in the
deduplication set. If Socket Mode is broken, the poller catches up
within `interval_seconds`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from slackbot.registry import Registry

log = logging.getLogger(__name__)

DeliverFn = Callable[[str, str, str, str], Awaitable[None]]

# On daemon restart, look this far back when initializing per-thread cursors.
# Any replies Slack received while the daemon was down inside this window will
# be replayed; the outer delivery path de-dupes by msg_ts so this is safe.
_RESTART_REPLAY_SECONDS = 300.0


class _WebClientProto(Protocol):
    async def conversations_replies(self, **kwargs: object) -> dict: ...


class SlackPoller:
    def __init__(
        self,
        reg: Registry,
        client: _WebClientProto,
        deliver: DeliverFn,
        interval_seconds: float = 15.0,
    ) -> None:
        self._reg = reg
        self._client = client
        self._deliver = deliver
        self._interval = interval_seconds
        # Per-thread cursor: only process messages with ts > seen_after[thread_ts].
        # Initialized to "now" at startup so we don't replay history.
        self._seen_after: dict[str, float] = {}

    def baseline_now(self) -> None:
        """Initialize per-thread cursors at startup.

        Baseline is `now - _RESTART_REPLAY_SECONDS` rather than `now` so that
        replies Slack delivered while the daemon was down (inside the replay
        window) still get picked up. The dedupe set in the delivery callback
        suppresses anything Bolt also delivered, so the redundant work is
        bounded and safe.
        """
        baseline = time.time() - _RESTART_REPLAY_SECONDS
        for sess in self._reg.list_threads():
            if sess.slack_thread_ts:
                self._seen_after[sess.slack_thread_ts] = baseline

    async def run(self) -> None:
        self.baseline_now()
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poller iteration failed")
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        threads = self._reg.list_threads()
        for sess in threads:
            thread_ts = sess.slack_thread_ts
            channel = sess.slack_channel
            if not thread_ts or not channel:
                continue
            await self._poll_thread(channel, thread_ts)

    async def _poll_thread(self, channel: str, thread_ts: str) -> None:
        cursor = self._seen_after.get(thread_ts, time.time())
        try:
            resp = await self._client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                oldest=f"{cursor:.6f}",
                inclusive=False,
                limit=50,
            )
        except Exception as exc:
            log.warning("conversations.replies failed for %s: %s", thread_ts, exc)
            return
        messages = resp.get("messages") or []
        # Slack returns the thread parent first; skip anything that isn't a
        # legitimate user reply.
        max_ts = cursor
        for msg in messages:
            if msg.get("bot_id"):
                continue
            if msg.get("subtype"):  # message_changed, thread_broadcast headers, etc.
                continue
            ts_str = msg.get("ts")
            if not ts_str:
                continue
            try:
                ts_f = float(ts_str)
            except (TypeError, ValueError):
                continue
            if ts_f <= cursor:
                continue
            max_ts = max(max_ts, ts_f)
            text = msg.get("text", "")
            log.info(
                "poller delivering missed reply: thread_ts=%s msg_ts=%s len=%d",
                thread_ts,
                ts_str,
                len(text),
            )
            await self._deliver(channel, thread_ts, text, ts_str)
        if max_ts > cursor:
            self._seen_after[thread_ts] = max_ts
