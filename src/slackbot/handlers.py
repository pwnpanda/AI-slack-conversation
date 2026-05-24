"""Wires hook events through the registry to Slack."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from slackbot.events import format_event, top_level_text
from slackbot.registry import Registry, Session

log = logging.getLogger(__name__)


class _SlackIOProto(Protocol):
    async def post_top_level(self, text: str) -> str: ...
    async def post_in_thread(self, thread_ts: str, text: str) -> str: ...
    async def edit_top_level(self, ts: str, text: str) -> None: ...
    async def react(self, ts: str, emoji: str) -> None: ...


class EventHandlers:
    def __init__(self, reg: Registry, slack: _SlackIOProto) -> None:
        self._reg = reg
        self._slack = slack

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

    async def _on_start(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        prior = self._reg.get_session(sid)
        self._reg.upsert_session(sid, ev["cwd"], ev.get("zellij_session"), ev.get("zellij_pane_id"))
        if prior and prior.name and prior.slack_thread_ts:
            await self._slack.edit_top_level(
                prior.slack_thread_ts,
                top_level_text(prior.name, prior.cwd, "active"),
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
            prior_thread = self._reg.claim_name(sid, new_name)
            sess = self._reg.get_session(sid)
            assert sess is not None
            if prior_thread:
                await self._slack.post_in_thread(
                    prior_thread,
                    f"─── 🔄 resumed in new session @ {_iso_now()} ───",
                )
            else:
                ts = await self._slack.post_top_level(top_level_text(new_name, sess.cwd, "active"))
                self._reg.set_thread_ts(sid, ts)
                sess = self._reg.get_session(sid)
                assert sess is not None
            await self._drain_buffer(sess)
        else:
            self._reg.set_name(sid, new_name)
            if sess.slack_thread_ts:
                await self._slack.edit_top_level(
                    sess.slack_thread_ts,
                    top_level_text(new_name, sess.cwd, sess.status),
                )

    async def _on_prompt(self, ev: dict[str, Any]) -> None:
        await self._post_or_buffer(ev["session_id"], "prompt", {"text": ev.get("text", "")})

    async def _on_response(self, ev: dict[str, Any]) -> None:
        data = {"text": ev.get("text", ""), "tool_summary": ev.get("tool_summary")}
        await self._post_or_buffer(ev["session_id"], "response", data)

    async def _on_notification(self, ev: dict[str, Any]) -> None:
        await self._post_or_buffer(
            ev["session_id"], "notification", {"message": ev.get("message", "")}
        )

    async def _on_error(self, ev: dict[str, Any]) -> None:
        await self._post_or_buffer(ev["session_id"], "error", {"text": ev.get("text", "")})

    async def _on_end(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        self._reg.set_status(sid, "ended")
        sess = self._reg.get_session(sid)
        if sess and sess.name and sess.slack_thread_ts:
            await self._slack.edit_top_level(
                sess.slack_thread_ts, top_level_text(sess.name, sess.cwd, "ended")
            )

    async def _post_or_buffer(self, sid: str, kind: str, data: dict[str, Any]) -> None:
        sess = self._reg.get_session(sid)
        if sess is None:
            log.warning("event for unknown session %s", sid)
            return
        payload = json.dumps(data)
        if sess.name is None or sess.slack_thread_ts is None:
            self._reg.buffer_event(sid, kind, payload)
            return
        text = format_event(kind, data)
        ts = await self._slack.post_in_thread(sess.slack_thread_ts, text)
        evt_id = self._reg.buffer_event(sid, kind, payload)
        self._reg.mark_event_posted(evt_id, ts)

    async def _drain_buffer(self, sess: Session) -> None:
        assert sess.slack_thread_ts is not None
        for ev in self._reg.drain_unposted(sess.cc_session_id):
            data = json.loads(ev.payload)
            ts = await self._slack.post_in_thread(sess.slack_thread_ts, format_event(ev.kind, data))
            self._reg.mark_event_posted(ev.id, ts)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
