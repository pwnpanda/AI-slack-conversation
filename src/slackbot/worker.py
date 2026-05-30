"""Per-conversation worker. Owns the queue, mirrors transcript events to
Matrix, delivers Matrix replies into the pane, suppresses delivery echo."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol

from slackbot.events import format_event
from slackbot.registry import Registry

log = logging.getLogger(__name__)

# Cap how many uuids we remember per worker so memory stays bounded.
_MAX_REMEMBERED_UUIDS = 1000
# Cap on pending echo entries.
_MAX_PENDING_ECHO = 64


class _MatrixIOProto(Protocol):
    def room_for_agent(self, agent: str) -> str: ...
    async def post_top_level(self, text: str, room_id: str | None = None) -> str: ...
    async def post_in_thread(
        self, thread_root: str, text: str, room_id: str | None = None
    ) -> str: ...
    async def edit_top_level(self, ts: str, text: str, room_id: str | None = None) -> None: ...
    async def react(self, ts: str, emoji: str, room_id: str | None = None) -> None: ...


class _ActuatorProto(Protocol):
    async def deliver(self, session: str, pane_id: str, text: str) -> None: ...


class Worker:
    def __init__(
        self,
        sid: str,
        reg: Registry,
        matrix: _MatrixIOProto,
        actuator: _ActuatorProto,
    ) -> None:
        self._sid = sid
        self._reg = reg
        self._matrix = matrix
        self._actuator = actuator
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._posted_uuids: list[str] = []  # FIFO bounded
        self._pending_echo: list[str] = []  # FIFO bounded
        # Pending notification (for resolved-marker editing) is persisted in
        # the registry, NOT in-memory — survives worker reap + daemon restart.

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def enqueue(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)

    async def stop(self) -> None:
        """Drain the queue, then cancel the task."""
        await self._queue.join()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._dispatch(event)
            except Exception:
                log.exception("worker[%s] dispatch failed for %r", self._sid, event)
            finally:
                self._queue.task_done()

    async def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        method = getattr(self, f"_on_{kind}", None)
        if method is None:
            log.debug("worker[%s] no handler for kind=%s", self._sid, kind)
            return
        await method(event)

    async def _on_prompt(self, ev: dict[str, Any]) -> None:
        text = ev.get("text", "")
        if text in self._pending_echo:
            self._pending_echo.remove(text)
            log.debug("worker[%s] echo suppressed: %r", self._sid, text)
            return
        await self._mark_pending_notification_resolved()
        await self._mirror("prompt", {"text": text}, ev.get("uuid"))

    async def _on_response(self, ev: dict[str, Any]) -> None:
        uuid = ev.get("uuid")
        if uuid in self._posted_uuids:
            return
        await self._mark_pending_notification_resolved()
        data: dict[str, Any] = {"text": ev.get("text", "")}
        tool_summary = ev.get("tool_summary")
        if tool_summary:
            data["tool_summary"] = tool_summary
        await self._mirror("response", data, uuid)

    async def _on_notification(self, ev: dict[str, Any]) -> None:
        sess = self._reg.get_session(self._sid)
        if sess is None:
            return
        data = {
            "message": ev.get("message", ""),
            "tool_request": ev.get("tool_request", ""),
            "context": ev.get("context", ""),
            "agent": sess.agent,
        }
        if sess.name is None or sess.matrix_thread_root is None:
            self._reg.buffer_event(self._sid, "notification", json.dumps(data))
            return
        await self._mark_pending_notification_resolved()
        text = format_event("notification", data)
        event_id = await self._matrix.post_in_thread(
            sess.matrix_thread_root, text, room_id=sess.matrix_room_id
        )
        # Persist so the resolved-marker edit survives reaping + restart.
        self._reg.set_pending_notification(self._sid, event_id, text, sess.matrix_room_id)

    async def _on_error(self, ev: dict[str, Any]) -> None:
        await self._mark_pending_notification_resolved()
        await self._mirror("error", {"text": ev.get("text", "")}, uuid=None)

    async def _on_matrix_reply(self, ev: dict[str, Any]) -> None:
        text = ev["text"]
        msg_ts = ev.get("msg_ts", "")
        sess = self._reg.get_session(self._sid)
        if sess is None:
            return
        room_id = sess.matrix_room_id
        if not sess.zellij_session or not sess.zellij_pane_id:
            await self._matrix.post_in_thread(
                sess.matrix_thread_root or "",
                "❌ delivery failed: session has no pane info",
                room_id=room_id,
            )
            return
        try:
            await self._actuator.deliver(sess.zellij_session, sess.zellij_pane_id, text)
        except Exception as exc:
            await self._matrix.post_in_thread(
                sess.matrix_thread_root or "",
                f"❌ delivery failed: {exc}",
                room_id=room_id,
            )
            await self._matrix.react(msg_ts, "warning", room_id=room_id)
            return
        self._remember_echo(text)
        await self._mark_pending_notification_resolved()
        await self._matrix.react(msg_ts, "white_check_mark", room_id=room_id)

    async def _mark_pending_notification_resolved(self) -> None:
        """Edit the previously posted notification to indicate it's no longer
        pending — the user already answered in the pane (or here via reply)."""
        pending = self._reg.consume_pending_notification(self._sid)
        if pending is None:
            return
        try:
            await self._matrix.edit_top_level(
                pending["ts"],
                pending["text"] + "\n_— resolved —_",
                room_id=(pending.get("room_id") or None),
            )
        except Exception:
            log.exception("worker[%s] failed to mark notification resolved", self._sid)

    async def _mirror(self, kind: str, data: dict[str, Any], uuid: str | None) -> None:
        sess = self._reg.get_session(self._sid)
        if sess is None:
            return
        if sess.name is None or sess.matrix_thread_root is None:
            # Buffer for replay when /rn or auto-recovery binds the thread.
            self._reg.buffer_event(self._sid, kind, json.dumps({**data, "agent": sess.agent}))
            return
        text = format_event(kind, {**data, "agent": sess.agent})
        await self._matrix.post_in_thread(
            sess.matrix_thread_root, text, room_id=sess.matrix_room_id
        )
        if uuid:
            self._remember_uuid(uuid)

    def _remember_uuid(self, uuid: str) -> None:
        self._posted_uuids.append(uuid)
        if len(self._posted_uuids) > _MAX_REMEMBERED_UUIDS:
            self._posted_uuids = self._posted_uuids[-_MAX_REMEMBERED_UUIDS:]

    def _remember_echo(self, text: str) -> None:
        self._pending_echo.append(text)
        if len(self._pending_echo) > _MAX_PENDING_ECHO:
            self._pending_echo = self._pending_echo[-_MAX_PENDING_ECHO:]
