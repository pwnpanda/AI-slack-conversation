"""Slack thread replies are enqueued into the matching worker; the worker
owns delivery, echo suppression, and reaction."""

from __future__ import annotations

import logging

from slackbot.liveness_cache import LivenessCache
from slackbot.registry import Registry
from slackbot.supervisor import Supervisor

log = logging.getLogger(__name__)


class ReplyRouter:
    def __init__(
        self,
        reg: Registry,
        supervisor: Supervisor,
        liveness: LivenessCache,
        slack,
    ) -> None:
        self._reg = reg
        self._sup = supervisor
        self._liveness = liveness
        self._slack = slack

    async def on_reply(self, channel: str, thread_ts: str, text: str, msg_ts: str) -> None:
        sess = self._reg.get_session_by_thread(thread_ts, channel)
        if sess is None:
            log.debug("reply for unknown thread %s ignored", thread_ts)
            return
        if not await self._liveness.is_alive(sess.cc_session_id, sess.cc_pid, sess.name):
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
