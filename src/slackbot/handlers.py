"""HTTP event handlers: turn POSTed hook events into supervisor calls."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from slackbot.events import top_level_text
from slackbot.process_liveness import session_is_alive
from slackbot.registry import Registry
from slackbot.supervisor import Supervisor

log = logging.getLogger(__name__)


class EventHandlers:
    def __init__(self, reg: Registry, supervisor: Supervisor, matrix) -> None:
        self._reg = reg
        self._sup = supervisor
        self._matrix = matrix
        # Strong refs to in-flight background edit tasks so asyncio doesn't
        # garbage-collect them mid-await (RUF006).
        self._inflight_edits: set[asyncio.Task] = set()

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        sid = event.get("session_id", "")
        if not sid:
            log.warning("event missing session_id: %r", event)
            return
        method = getattr(self, f"_on_{kind}", None)
        if method is None:
            log.warning("no handler for kind=%s", kind)
            return
        await method(event)
        await self._sup.touch(sid)

    async def _on_start(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        prior = self._reg.get_session(sid)
        agent = _agent(ev.get("agent"))
        room_id = self._matrix.room_for_agent(agent)
        cwd = ev["cwd"]
        zellij_session = ev.get("zellij_session")
        cc_pid = _int_or_none(ev.get("cc_pid"))
        transcript_path = ev.get("transcript_path") or None

        self._reg.upsert_session(
            sid,
            cwd,
            zellij_session,
            ev.get("zellij_pane_id"),
            agent=agent,
            matrix_room_id=room_id,
            cc_pid=cc_pid,
            transcript_path=transcript_path,
        )

        if transcript_path:
            self._sup.attach_reader(sid, transcript_path)
        await self._sup.get_or_create(sid)

        if prior and prior.name and prior.matrix_thread_root:
            await self._matrix.edit_top_level(
                prior.matrix_thread_root,
                top_level_text(prior.name, prior.cwd, "active", prior.agent),
                room_id=prior.matrix_room_id,
            )
            return

        recovered = self._reg.find_recoverable_session(
            zellij_session=zellij_session,
            zellij_pane_id=ev.get("zellij_pane_id"),
            cwd=cwd,
            agent=agent,
            exclude_sid=sid,
        )
        if (
            recovered
            and recovered.name
            and not session_is_alive(recovered.cc_session_id, recovered.cc_pid, recovered.name)
        ):
            await self._on_name(
                {
                    "kind": "name",
                    "session_id": sid,
                    "name": recovered.name,
                    "auto_recovered": True,
                }
            )
            return

        if _auto_registers(agent):
            await self._on_name(
                {
                    "kind": "name",
                    "session_id": sid,
                    "name": _auto_name(agent, cwd, sid),
                }
            )

    async def _on_name(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        new_name = ev["name"]
        sess = self._reg.get_session(sid)
        if sess is None:
            log.warning("name event for unknown session %s", sid)
            return
        if sess.name == new_name:
            return

        if sess.name is None:
            # First time naming this session: claim across the registry.
            prior_thread = self._reg.claim_name(sid, new_name)
            sess = self._reg.get_session(sid)
            assert sess is not None
            if prior_thread:
                marker = "auto-rebound" if ev.get("auto_recovered") else "resumed"
                await self._matrix.post_in_thread(
                    prior_thread,
                    f"─── 🔄 {marker} in new session @ {_iso_now()} ───",
                    room_id=sess.matrix_room_id,
                )
            else:
                event_id = await self._matrix.post_top_level(
                    top_level_text(new_name, sess.cwd, "active", sess.agent),
                    room_id=sess.matrix_room_id,
                )
                self._reg.set_matrix_thread_root(sid, event_id)

            # Replay buffered events into the worker.
            worker = await self._sup.get_or_create(sid)
            for buffered in self._reg.drain_unposted(sid):
                data = json.loads(buffered.payload)
                replay = {"kind": buffered.kind, **data}
                await worker.enqueue(replay)
                self._reg.mark_event_posted(buffered.id, "replayed")
        else:
            # Rename: update the existing thread header in-place.
            self._reg.set_name(sid, new_name)
            if sess.matrix_thread_root:
                await self._matrix.edit_top_level(
                    sess.matrix_thread_root,
                    top_level_text(new_name, sess.cwd, sess.status, sess.agent),
                    room_id=sess.matrix_room_id,
                )

    async def _on_end(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        self._reg.set_status(sid, "ended")
        sess = self._reg.get_session(sid)
        if sess and sess.name and sess.matrix_thread_root:
            await self._matrix.edit_top_level(
                sess.matrix_thread_root,
                top_level_text(sess.name, sess.cwd, "ended", sess.agent),
                room_id=sess.matrix_room_id,
            )
        self._sup.detach_reader(sid)

    async def _on_notification(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        # Refresh runtime fields opportunistically (cc_pid keeps drift in check).
        self._refresh_runtime(sid, ev)
        worker = await self._sup.get_or_create(sid)
        await worker.enqueue(
            {
                "kind": "notification",
                "message": ev.get("message", ""),
                "tool_request": ev.get("tool_request", ""),
                "context": ev.get("context", ""),
            }
        )

    async def _on_prompt(self, ev: dict[str, Any]) -> None:
        # Legacy hook. Transcript reader is the real source for prompt mirroring.
        # We still accept the event to refresh runtime fields (cc_pid, pane id).
        self._refresh_runtime(ev["session_id"], ev)

    async def _on_response(self, ev: dict[str, Any]) -> None:
        # Same rationale as _on_prompt — transcript reader does the mirroring.
        self._refresh_runtime(ev["session_id"], ev)

    async def _on_error(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        self._refresh_runtime(sid, ev)
        worker = await self._sup.get_or_create(sid)
        await worker.enqueue({"kind": "error", "text": ev.get("text", "")})

    def _refresh_runtime(self, sid: str, ev: dict[str, Any]) -> None:
        sess = self._reg.get_session(sid)
        if sess is None:
            # First hook arrival for a CC that pre-dates the current registry
            # (typical after the Slack→Matrix migration that dropped the DB,
            # or any other registry reset). Stub a row so subsequent events
            # can mirror; cwd is unknown until the next session_start fires.
            agent = _agent(ev.get("agent"))
            room_id = self._matrix.room_for_agent(agent)
            self._reg.upsert_session(
                sid,
                ev.get("cwd") or "(unknown)",
                ev.get("zellij_session"),
                ev.get("zellij_pane_id"),
                agent=agent,
                matrix_room_id=room_id,
                cc_pid=_int_or_none(ev.get("cc_pid")),
                transcript_path=ev.get("transcript_path"),
            )
            sess = self._reg.get_session(sid)
            log.info("auto-created session row for unknown sid %s (agent=%s)", sid, agent)
            if sess is None:
                return
        cc_pid = _int_or_none(ev.get("cc_pid"))
        self._reg.refresh_liveness(
            sid,
            ev.get("zellij_session") or sess.zellij_session,
            ev.get("zellij_pane_id") or sess.zellij_pane_id,
            cc_pid,
        )
        # Back-fill cwd when a later hook supplies a real value. Top-level
        # message may already exist with the placeholder; if so, edit it
        # in place rather than leaving "(unknown)" forever.
        new_cwd = ev.get("cwd")
        if new_cwd and sess.cwd in ("(unknown)", "") and new_cwd != sess.cwd:
            self._reg.upsert_session(
                sid,
                new_cwd,
                sess.zellij_session,
                sess.zellij_pane_id,
                agent=sess.agent,
                matrix_room_id=sess.matrix_room_id,
                cc_pid=cc_pid,
                transcript_path=sess.transcript_path,
            )
            if sess.name and sess.matrix_thread_root:
                refreshed = self._reg.get_session(sid)
                if refreshed:
                    # Hold the task reference (RUF006) so it isn't GC'd mid-flight.
                    task = asyncio.create_task(self._edit_top_level_with_new_cwd(refreshed))
                    self._inflight_edits.add(task)
                    task.add_done_callback(self._inflight_edits.discard)
        # Self-heal: an event arriving for a session previously marked ended
        # means CC came back (auto-resume, manual restart). Flip status back
        # and re-attach the transcript reader if needed. Without this, a
        # stale session_end from an earlier exit can permanently mute the
        # session even though CC is producing fresh transcript content.
        if sess.status == "ended":
            self._reg.set_status(sid, "active")
        if sess.transcript_path:
            self._sup.attach_reader(sid, sess.transcript_path)

    async def _edit_top_level_with_new_cwd(self, sess) -> None:
        try:
            await self._matrix.edit_top_level(
                sess.matrix_thread_root,
                top_level_text(sess.name, sess.cwd, sess.status, sess.agent),
                room_id=sess.matrix_room_id,
            )
        except Exception:
            log.exception("failed to re-edit top-level after cwd backfill")


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _agent(value: object) -> str:
    agent = str(value or "claude").lower()
    if agent in {"claude", "codex", "gemini"}:
        return agent
    return "claude"


def _auto_registers(agent: str) -> bool:
    return agent in {"codex", "gemini"}


def _auto_name(agent: str, cwd: str, sid: str) -> str:
    project = PurePath(cwd).name or "session"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", project).strip("-").lower() or "session"
    short_sid = re.sub(r"[^a-zA-Z0-9]+", "", sid)[:8] or "session"
    return f"{agent}-{slug}-{short_sid}"


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
