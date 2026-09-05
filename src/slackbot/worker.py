"""Per-conversation worker. Owns the queue, mirrors transcript events to
Matrix, delivers Matrix replies into the pane, suppresses delivery echo."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol

from slackbot.events import (
    chunk_for_matrix,
    format_event,
    parse_ask_user_question,
    top_level_text,
)
from slackbot.registry import Registry

log = logging.getLogger(__name__)

# Cap how many uuids we remember per worker so memory stays bounded.
_MAX_REMEMBERED_UUIDS = 1000
# Cap on pending echo entries.
_MAX_PENDING_ECHO = 64
# How long the top-level "recent activity" marker (🟡) stays before
# reverting to the steady-state 🟢. Any new bot post within this window
# resets the timer.
_RECENT_MARKER_SECONDS = 60.0


def _option_index_for_reply(text: str, pending: dict | None) -> int | None:
    """Return zero-based option index if *text* is a bare option number and a
    question is pending with that option. None means "deliver as text"."""
    if pending is None:
        return None
    options = pending.get("options") if isinstance(pending, dict) else None
    if not isinstance(options, list) or not options:
        return None
    stripped = text.strip()
    if not stripped.isdigit():
        return None
    idx = int(stripped) - 1
    if 0 <= idx < len(options):
        return idx
    return None


def _echo_key(text: str) -> str:
    """Normalize text for echo matching.

    The actuator delivers the raw Matrix body, but CC strips surrounding
    whitespace before recording the prompt in its transcript. Element X also
    appends a trailing space to message bodies. Comparing the raw strings
    therefore never matches, so every delivered reply gets mirrored back as an
    echo. Stripping both sides makes the comparison survive that delta.
    """
    return text.strip()


class _MatrixIOProto(Protocol):
    def room_for_agent(self, agent: str) -> str: ...
    def has_user_client(self) -> bool: ...
    async def post_top_level(
        self, text: str, room_id: str | None = None, as_user: bool = False
    ) -> str: ...
    async def post_in_thread(
        self,
        thread_root: str,
        text: str,
        room_id: str | None = None,
        as_user: bool = False,
    ) -> str: ...
    async def edit_top_level(self, ts: str, text: str, room_id: str | None = None) -> None: ...
    async def react(self, ts: str, emoji: str, room_id: str | None = None) -> None: ...


class _ActuatorProto(Protocol):
    async def deliver(self, session: str, pane_id: str, text: str) -> None: ...
    async def deliver_keys(self, session: str, pane_id: str, keys: list[str]) -> None: ...


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
        # "Recent activity" marker state on the top-level message. _fresh
        # is True while the top-level shows 🟡; _stale_task is the
        # asyncio.Task scheduled to flip back to 🟢 after silence.
        self._fresh: bool = False
        self._stale_task: asyncio.Task | None = None
        self._inflight_marker_tasks: set[asyncio.Task] = set()
        # Pending notification (for resolved-marker editing) is persisted in
        # the registry, NOT in-memory — survives worker reap + daemon restart.

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def enqueue(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)

    async def stop(self) -> None:
        """Drain the queue, then cancel the task and any pending marker timers."""
        await self._queue.join()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # Cancel any pending recent-marker revert tasks so they don't leak
        # past worker stop / event-loop teardown.
        for task in list(self._inflight_marker_tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
        key = _echo_key(text)
        if key in self._pending_echo:
            self._pending_echo.remove(key)
            log.debug("worker[%s] echo suppressed: %r", self._sid, text)
            return
        await self._mark_pending_notification_resolved()
        await self._mirror("prompt", {"text": text}, ev.get("uuid"))

    async def _on_response(self, ev: dict[str, Any]) -> None:
        uuid = ev.get("uuid")
        if uuid in self._posted_uuids:
            return
        await self._mark_pending_notification_resolved()
        # A fresh assistant response means CC consumed the prior question's
        # answer; any stale pending_question state is no longer relevant.
        self._reg.clear_pending_question(self._sid)
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
        bound = await self._ensure_thread_root(sess)
        if bound is None:
            self._reg.buffer_event(self._sid, "notification", json.dumps(data))
            return
        sess = bound
        await self._mark_pending_notification_resolved()
        text = format_event("notification", data)
        chunks = chunk_for_matrix(text)
        first_event_id = None
        for chunk in chunks:
            event_id = await self._matrix.post_in_thread(
                sess.matrix_thread_root, chunk, room_id=sess.matrix_room_id
            )
            if first_event_id is None:
                first_event_id = event_id
        # Persist the first chunk's event_id so the resolved-marker edit
        # lands on the head of the notification (not a continuation chunk).
        assert first_event_id is not None
        self._reg.set_pending_notification(
            self._sid, first_event_id, chunks[0], sess.matrix_room_id
        )
        # If this notification is an AskUserQuestion, remember the option list
        # so a numeric reply navigates by arrow keys rather than landing in
        # the "Other" text field. Any other notification clears the state.
        question = parse_ask_user_question(str(ev.get("tool_request", "")))
        if question is not None:
            self._reg.set_pending_question(self._sid, question["options"])
        else:
            self._reg.clear_pending_question(self._sid)
        await self._mark_thread_fresh()

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
        # If CC is mid-AskUserQuestion and the reply is a bare option number,
        # navigate the TUI with arrow keys instead of typing the digit (which
        # would land in the "Other" free-text field on CC's question UI).
        pending = self._reg.get_pending_question(self._sid)
        option_index = _option_index_for_reply(text, pending)
        try:
            if option_index is not None:
                keys = ["Down"] * option_index + ["Enter"]
                await self._actuator.deliver_keys(sess.zellij_session, sess.zellij_pane_id, keys)
                # CC's transcript records the selected option's LABEL as a
                # user prompt, not the digit we received from Matrix.
                # Pre-stage that label in the echo set so the resulting
                # prompt event isn't mirrored back here as a duplicate
                # 👤 message of what the user just answered.
                if pending is not None:
                    options = pending.get("options") or []
                    if 0 <= option_index < len(options):
                        label = options[option_index].get("label") or ""
                        if label:
                            self._remember_echo(label)
                self._reg.clear_pending_question(self._sid)
            else:
                await self._actuator.deliver(sess.zellij_session, sess.zellij_pane_id, text)
                # Free-form text answer goes into the "Other" field if a
                # question was pending; clear state so the next reply isn't
                # mis-routed if CC actually moved past the question.
                if pending is not None:
                    self._reg.clear_pending_question(self._sid)
        except Exception as exc:
            await self._matrix.post_in_thread(
                sess.matrix_thread_root or "",
                f"❌ delivery failed: {exc}",
                room_id=room_id,
            )
            await self._matrix.react(msg_ts, "warning", room_id=room_id)
            return
        if option_index is None:
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

    async def _ensure_thread_root(self, sess: Any) -> Any | None:
        """Return the session with a thread root guaranteed, or None if it
        cannot have one yet because the session is still unnamed.

        A top-level is normally posted once, when a session is first named. A
        session that changes rooms has to drop its root — the root event only
        exists in the old room — and the naming path never fires again for an
        already-named session. Without recreating it here such a session would
        buffer its output forever, which is exactly what a room switch caused.
        """
        if sess.matrix_thread_root is not None:
            return sess
        if sess.name is None:
            return None
        event_id = await self._matrix.post_top_level(
            top_level_text(sess.name, sess.cwd, sess.status, sess.agent),
            room_id=sess.matrix_room_id,
        )
        self._reg.set_matrix_thread_root(self._sid, event_id)
        log.info(
            "worker[%s] recreated top-level %s in %s",
            self._sid,
            event_id,
            sess.matrix_room_id,
        )
        refreshed = self._reg.get_session(self._sid)
        if refreshed is not None:
            await self._replay_buffered(refreshed)
        return refreshed

    async def _replay_buffered(self, sess: Any) -> None:
        """Post everything buffered while the session had no thread, oldest
        first, so the recreated thread keeps the output it accumulated."""
        for buffered in self._reg.drain_unposted(self._sid):
            data = json.loads(buffered.payload)
            for chunk in chunk_for_matrix(format_event(buffered.kind, data)):
                await self._matrix.post_in_thread(
                    sess.matrix_thread_root, chunk, room_id=sess.matrix_room_id
                )
            self._reg.mark_event_posted(buffered.id, "replayed")

    async def _mirror(self, kind: str, data: dict[str, Any], uuid: str | None) -> None:
        sess = self._reg.get_session(self._sid)
        if sess is None:
            return
        bound = await self._ensure_thread_root(sess)
        if bound is None:
            # Still unnamed: buffer for replay when /rn binds the thread.
            self._reg.buffer_event(self._sid, kind, json.dumps({**data, "agent": sess.agent}))
            return
        sess = bound
        # Prompts originate from the human; if a user-puppet client is wired
        # up, post them under the user's identity with no '[Claude] 👤'
        # prefix — Element shows the user's avatar/displayname inline so
        # the agent-label is redundant and the human-outline emoji is
        # double-redundant. Bot output (responses, errors, notifications)
        # keeps its prefix and posts as the bot account.
        as_user = kind == "prompt" and self._matrix.has_user_client()
        if as_user:
            text = str(data.get("text", ""))
        else:
            text = format_event(kind, {**data, "agent": sess.agent})
        for chunk in chunk_for_matrix(text):
            await self._matrix.post_in_thread(
                sess.matrix_thread_root,
                chunk,
                room_id=sess.matrix_room_id,
                as_user=as_user,
            )
        if uuid:
            self._remember_uuid(uuid)
        await self._mark_thread_fresh()

    async def _mark_thread_fresh(self) -> None:
        """Switch the top-level marker to 🟡 (if not already) and schedule a
        revert to 🟢 after silence.

        Edit-on-burst-start: any subsequent posts within the window only
        reset the timer, not re-edit. Edit-on-burst-end runs once per burst.
        Skips the edit if the session isn't named / thread-bound — there's
        no top-level to mark.
        """
        sess = self._reg.get_session(self._sid)
        if sess is None or sess.name is None or sess.matrix_thread_root is None:
            return
        if sess.status != "active":
            return
        # Cancel any pending revert; this burst extends the window.
        if self._stale_task is not None and not self._stale_task.done():
            self._stale_task.cancel()
        if not self._fresh:
            try:
                await self._matrix.edit_top_level(
                    sess.matrix_thread_root,
                    top_level_text(sess.name, sess.cwd, sess.status, sess.agent, recent=True),
                    room_id=sess.matrix_room_id,
                )
                self._fresh = True
            except Exception:
                log.exception("worker[%s] failed to mark thread fresh", self._sid)
                return
        # Schedule the revert. Hold a strong ref (RUF006) so asyncio doesn't
        # garbage-collect the task mid-sleep.
        task = asyncio.create_task(self._revert_thread_marker_after_delay())
        self._stale_task = task
        self._inflight_marker_tasks.add(task)
        task.add_done_callback(self._inflight_marker_tasks.discard)

    async def _revert_thread_marker_after_delay(self) -> None:
        try:
            await asyncio.sleep(_RECENT_MARKER_SECONDS)
        except asyncio.CancelledError:
            return
        sess = self._reg.get_session(self._sid)
        if sess is None or sess.name is None or sess.matrix_thread_root is None:
            self._fresh = False
            return
        try:
            await self._matrix.edit_top_level(
                sess.matrix_thread_root,
                top_level_text(sess.name, sess.cwd, sess.status, sess.agent, recent=False),
                room_id=sess.matrix_room_id,
            )
        except Exception:
            log.exception("worker[%s] failed to revert thread marker", self._sid)
        finally:
            self._fresh = False

    def _remember_uuid(self, uuid: str) -> None:
        self._posted_uuids.append(uuid)
        if len(self._posted_uuids) > _MAX_REMEMBERED_UUIDS:
            self._posted_uuids = self._posted_uuids[-_MAX_REMEMBERED_UUIDS:]

    def _remember_echo(self, text: str) -> None:
        self._pending_echo.append(_echo_key(text))
        if len(self._pending_echo) > _MAX_PENDING_ECHO:
            self._pending_echo = self._pending_echo[-_MAX_PENDING_ECHO:]
