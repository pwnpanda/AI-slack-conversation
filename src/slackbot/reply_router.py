"""Matrix thread replies are enqueued into the matching worker; the worker
owns delivery, echo suppression, and reaction."""

from __future__ import annotations

import logging

from slackbot.registry import Registry
from slackbot.supervisor import Supervisor

log = logging.getLogger(__name__)


class ReplyRouter:
    """Map a Matrix thread reply onto its session's worker.

    The actuator is the ground truth for delivery viability: if zellij
    can't write to the pane, the worker posts an `❌ delivery failed`
    message and a warning reaction. Doing an upfront liveness probe here
    was a guess (argv scan + dead cc_pid + idle hook timer) that produced
    false-positive rejections for valid sessions that happen to be idle.
    Let the actual write-chars call decide.
    """

    def __init__(
        self,
        reg: Registry,
        supervisor: Supervisor,
        matrix,
    ) -> None:
        self._reg = reg
        self._sup = supervisor
        self._matrix = matrix

    async def on_reply(self, room_id: str, thread_root: str, text: str, msg_ts: str) -> None:
        sess = self._reg.get_session_by_matrix_thread(thread_root, room_id)
        if sess is None:
            log.debug("reply for unknown thread %s ignored", thread_root)
            return
        worker = await self._sup.get_or_create(sess.cc_session_id)
        await worker.enqueue({"kind": "matrix_reply", "text": text, "msg_ts": msg_ts})
