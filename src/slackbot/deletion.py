"""Thread deletion: redact a whole thread and drop its registry binding.

Triggered by a 🗑️ reaction on a thread's top-level or a `/del` reply in
the thread (both wired in __main__). The bot holds PL100 in rooms it
created, so it can redact every event regardless of sender; redaction
removes the content server-side for all clients.
"""

from __future__ import annotations

import logging
from typing import Protocol

from slackbot.registry import Registry

log = logging.getLogger(__name__)


class _MatrixDeleteProto(Protocol):
    async def thread_event_ids(self, room_id: str, thread_root: str) -> list[str]: ...
    async def redact(self, room_id: str, event_id: str, reason: str = ...) -> None: ...


async def delete_thread(
    matrix: _MatrixDeleteProto, reg: Registry, room_id: str, thread_root: str
) -> int:
    """Redact the thread root + all descendants, then clear the session's
    name/thread binding so a still-live session starts fresh next time.
    Returns the count of events successfully redacted."""
    event_ids = await matrix.thread_event_ids(room_id, thread_root)
    redacted = 0
    for eid in event_ids:
        try:
            await matrix.redact(room_id, eid, reason="thread deleted by user")
            redacted += 1
        except Exception:
            log.exception("failed to redact %s in %s", eid, room_id)
    sess = reg.get_session_by_matrix_thread(thread_root, room_id)
    if sess is not None:
        reg.clear_thread_binding(sess.cc_session_id)
    log.info("deleted thread %s in %s (%d events redacted)", thread_root, room_id, redacted)
    return redacted
