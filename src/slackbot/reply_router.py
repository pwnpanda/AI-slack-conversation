"""Slack thread replies are enqueued into the matching worker; the worker
owns delivery, echo suppression, and reaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from slackbot.liveness_cache import LivenessCache
from slackbot.registry import Registry
from slackbot.supervisor import Supervisor

log = logging.getLogger(__name__)

# Recent hook activity (any prompt/response/notification POST from CC) is
# treated as proof of life: an alive CC produces hook events every few
# seconds. This catches CCs whose argv doesn't expose the session_id or
# --resume <name> tokens (e.g. session loaded via CC's interactive
# /resume), where the strict process scan would otherwise return False.
_RECENT_HOOK_WINDOW_SECONDS = 120


class ReplyRouter:
    def __init__(
        self,
        reg: Registry,
        supervisor: Supervisor,
        liveness: LivenessCache,
        slack,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._reg = reg
        self._sup = supervisor
        self._liveness = liveness
        self._slack = slack
        self._clock = clock

    async def on_reply(self, channel: str, thread_ts: str, text: str, msg_ts: str) -> None:
        sess = self._reg.get_session_by_thread(thread_ts, channel)
        if sess is None:
            log.debug("reply for unknown thread %s ignored", thread_ts)
            return
        recent_hook = self._clock() - sess.last_event_at < _RECENT_HOOK_WINDOW_SECONDS
        if not recent_hook and not await self._liveness.is_alive(
            sess.cc_session_id, sess.cc_pid, sess.name
        ):
            self._reg.set_status(sess.cc_session_id, "ended")
            await self._slack.post_in_thread(
                thread_ts,
                "⚠️ No running CC process found for this session. "
                "Reply not sent — start a new CC session in this workspace and "
                "it will auto-rebind to this thread.",
                channel=channel,
            )
            await self._slack.react(msg_ts, "no_entry_sign", channel=channel)
            return
        worker = await self._sup.get_or_create(sess.cc_session_id)
        await worker.enqueue({"kind": "slack_reply", "text": text, "msg_ts": msg_ts})
