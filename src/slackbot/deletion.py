"""Thread deletion: redact a whole thread and drop its registry binding.

Triggered by a 🗑️ reaction on a thread's top-level or a `/del` reply in
the thread (both wired in __main__). The bot holds PL100 in rooms it
created, so it can redact every event regardless of sender; redaction
removes the content server-side for all clients.

Deletions run as background tasks via ThreadDeleter: they are triggered from
Matrix sync callbacks, and a large thread means hundreds of sequential
redactions, during which no other message (replies, further /del) would be
handled.
"""

from __future__ import annotations

import asyncio
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


class ThreadDeleter:
    """Runs delete_thread in the background, one task per thread."""

    def __init__(self, matrix: _MatrixDeleteProto, reg: Registry) -> None:
        self._matrix = matrix
        self._reg = reg
        self._running: dict[tuple[str, str], asyncio.Task[int]] = {}

    def start(self, room_id: str, thread_root: str) -> bool:
        """Begin deleting the thread; False if it is already being deleted
        (e.g. a /del and a 🗑️ for the same thread)."""
        key = (room_id, thread_root)
        if key in self._running:
            log.info("thread %s in %s is already being deleted", thread_root, room_id)
            return False
        task = asyncio.create_task(delete_thread(self._matrix, self._reg, room_id, thread_root))
        self._running[key] = task
        task.add_done_callback(lambda t: self._finished(key, t))
        return True

    def _finished(self, key: tuple[str, str], task: asyncio.Task[int]) -> None:
        self._running.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            room_id, thread_root = key
            log.error(
                "failed to delete thread %s in %s",
                thread_root,
                room_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def wait_idle(self) -> None:
        """Wait for every running deletion to finish."""
        while self._running:
            await asyncio.wait(list(self._running.values()))
            await asyncio.sleep(0)  # let the done-callbacks drop finished tasks

    def cancel_all(self) -> None:
        for task in self._running.values():
            task.cancel()
