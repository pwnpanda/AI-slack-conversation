"""Wires hook events through the registry to Slack."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, Protocol

from slackbot.dedupe import DeliveryDedupe
from slackbot.events import format_event, top_level_text
from slackbot.registry import Registry, Session

log = logging.getLogger(__name__)


class _SlackIOProto(Protocol):
    def channel_for_agent(self, agent: str) -> str: ...
    async def post_top_level(self, text: str, channel: str | None = None) -> str: ...
    async def post_in_thread(
        self, thread_ts: str, text: str, channel: str | None = None
    ) -> str: ...
    async def edit_top_level(self, ts: str, text: str, channel: str | None = None) -> None: ...
    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None: ...


class EventHandlers:
    def __init__(
        self,
        reg: Registry,
        slack: _SlackIOProto,
        dedupe: DeliveryDedupe | None = None,
        stale_after_seconds: int = 21600,
    ) -> None:
        self._reg = reg
        self._slack = slack
        self._dedupe = dedupe
        self._stale_after = stale_after_seconds

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        sid = event.get("session_id", "")
        if not sid:
            log.warning("event missing session_id: %r", event)
            return
        # Refresh mutable runtime fields (pid, pane id) from any event other than
        # 'start' (which does its own full upsert). Lets a long-running CC heal
        # the registry after a zellij restart or after we added cc_pid to schema.
        if kind != "start" and self._reg.get_session(sid) is not None:
            cc_pid_raw = event.get("cc_pid")
            try:
                cc_pid_val = int(cc_pid_raw) if cc_pid_raw is not None else None
            except (TypeError, ValueError):
                cc_pid_val = None
            self._reg.refresh_liveness(
                sid,
                event.get("zellij_session"),
                event.get("zellij_pane_id"),
                cc_pid_val,
            )
        method = getattr(self, f"_on_{kind}", None)
        if method is None:
            log.warning("no handler for kind=%s", kind)
            return
        await method(event)

    async def _on_start(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        prior = self._reg.get_session(sid)
        agent = _agent(ev.get("agent"))
        channel = self._slack.channel_for_agent(agent)
        cwd = ev["cwd"]
        zellij_session = ev.get("zellij_session")
        cc_pid_raw = ev.get("cc_pid")
        try:
            cc_pid: int | None = int(cc_pid_raw) if cc_pid_raw is not None else None
        except (TypeError, ValueError):
            cc_pid = None
        self._reg.upsert_session(
            sid,
            cwd,
            zellij_session,
            ev.get("zellij_pane_id"),
            agent=agent,
            slack_channel=channel,
            cc_pid=cc_pid,
        )
        if prior and prior.name and prior.slack_thread_ts:
            prior_channel = prior.slack_channel
            await self._slack.edit_top_level(
                prior.slack_thread_ts,
                top_level_text(prior.name, prior.cwd, "active", prior.agent),
                channel=prior_channel,
            )
            return

        # Auto-recover: if a prior named session in the same (cwd, zellij_session,
        # agent) workspace is dead/stale, inherit its name and thread so the user
        # doesn't have to /rn after every CC restart.
        recovered = self._reg.find_recoverable_session(
            zellij_session=zellij_session,
            cwd=cwd,
            agent=agent,
            exclude_sid=sid,
        )
        if recovered and recovered.name and recovered.status == "ended":
            log.info(
                "auto-recovering name=%r from %s into new session %s (cwd=%s)",
                recovered.name,
                recovered.cc_session_id,
                sid,
                cwd,
            )
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
            prior_thread = self._reg.claim_name(sid, new_name)
            sess = self._reg.get_session(sid)
            assert sess is not None
            if prior_thread:
                marker = "auto-rebound" if ev.get("auto_recovered") else "resumed"
                await self._slack.post_in_thread(
                    prior_thread,
                    f"─── 🔄 {marker} in new session @ {_iso_now()} ───",
                    channel=sess.slack_channel,
                )
            else:
                ts = await self._slack.post_top_level(
                    top_level_text(new_name, sess.cwd, "active", sess.agent),
                    channel=sess.slack_channel,
                )
                self._reg.set_thread_ts(sid, ts)
                sess = self._reg.get_session(sid)
                assert sess is not None
            await self._drain_buffer(sess)
        else:
            self._reg.set_name(sid, new_name)
            if sess.slack_thread_ts:
                await self._slack.edit_top_level(
                    sess.slack_thread_ts,
                    top_level_text(new_name, sess.cwd, sess.status, sess.agent),
                    channel=sess.slack_channel,
                )

    async def _on_prompt(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        text = ev.get("text", "")
        if self._dedupe and self._dedupe.consume(sid, text):
            log.debug("prompt suppressed (originated from Slack delivery): %r", text)
            return
        await self._post_or_buffer(sid, "prompt", {"text": text})

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
                sess.slack_thread_ts,
                top_level_text(sess.name, sess.cwd, "ended", sess.agent),
                channel=sess.slack_channel,
            )

    async def _post_or_buffer(self, sid: str, kind: str, data: dict[str, Any]) -> None:
        sess = self._reg.get_session(sid)
        if sess is None:
            log.warning("event for unknown session %s", sid)
            return
        data = {**data, "agent": sess.agent}
        payload = json.dumps(data)
        if sess.name is None or sess.slack_thread_ts is None:
            self._reg.buffer_event(sid, kind, payload)
            return
        text = format_event(kind, data)
        ts = await self._slack.post_in_thread(
            sess.slack_thread_ts, text, channel=sess.slack_channel
        )
        evt_id = self._reg.buffer_event(sid, kind, payload)
        self._reg.mark_event_posted(evt_id, ts)

    async def _drain_buffer(self, sess: Session) -> None:
        assert sess.slack_thread_ts is not None
        for ev in self._reg.drain_unposted(sess.cc_session_id):
            data = json.loads(ev.payload)
            data = {**data, "agent": data.get("agent", sess.agent)}
            ts = await self._slack.post_in_thread(
                sess.slack_thread_ts,
                format_event(ev.kind, data),
                channel=sess.slack_channel,
            )
            self._reg.mark_event_posted(ev.id, ts)


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
