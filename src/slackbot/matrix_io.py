"""Thin async wrapper over nio.AsyncClient for room/thread ops.

Replaces the previous Slack transport. Callers treat the returned event
IDs as opaque strings; the public method names and signatures intentionally
mirror what the rest of the daemon expects so the call sites stay flat.
"""

from __future__ import annotations

import logging
from typing import Any

import markdown

log = logging.getLogger(__name__)


def _render_html(body: str) -> str:
    """CommonMark-ish render of *body* for Matrix's formatted_body field.

    `fenced_code` keeps triple-backtick blocks intact (CC output is often
    multi-line code/JSON we wrap in ``` already). `nl2br` converts single
    newlines into <br> so we don't have to double-newline every line in
    the bot's source text.
    """
    return markdown.markdown(body, extensions=["fenced_code", "nl2br"])


def _text_content(body: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an m.text content dict with both plain body + rendered HTML."""
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": _render_html(body),
    }
    if extra:
        content.update(extra)
    return content


# Slack emoji-name -> Matrix Unicode glyph. Element X renders these natively.
_EMOJI_MAP: dict[str, str] = {
    "white_check_mark": "✅",
    "warning": "⚠️",
    "no_entry_sign": "\U0001f6ab",
    "x": "❌",
    "hourglass_flowing_sand": "⏳",
}


def _emoji_for(name: str) -> str:
    """Map a Slack-style emoji name to its Unicode glyph.

    Unknown names fall through unchanged: Element X will render whatever
    string we send as the reaction key, so a missing mapping degrades to
    a literal text reaction rather than a crash.
    """
    return _EMOJI_MAP.get(name, name)


class MatrixIO:
    """Post, edit, and react in Matrix rooms via nio.AsyncClient."""

    def __init__(
        self,
        client: Any,
        room_id: str,
        agent_rooms: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._room_id = room_id
        self._agent_rooms = agent_rooms or {}

    def room_for_agent(self, agent: str) -> str:
        return self._agent_rooms.get(agent.lower(), self._room_id)

    def _room_or_default(self, room_id: str | None) -> str:
        return room_id or self._room_id

    async def post_top_level(self, text: str, room_id: str | None = None) -> str:
        target = self._room_or_default(room_id)
        resp = await self._client.room_send(
            room_id=target,
            message_type="m.room.message",
            content=_text_content(text),
        )
        return str(resp.event_id)

    async def post_in_thread(self, thread_root: str, text: str, room_id: str | None = None) -> str:
        """Post text as a threaded reply to the root event thread_root.

        Sends both the m.thread relation (for spec-compliant clients) and
        the m.in_reply_to / is_falling_back fields so clients that do not
        understand threads still render the message as a reply.
        """
        target = self._room_or_default(room_id)
        content = _text_content(
            text,
            extra={
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": thread_root,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": thread_root},
                }
            },
        )
        resp = await self._client.room_send(
            room_id=target,
            message_type="m.room.message",
            content=content,
        )
        return str(resp.event_id)

    async def edit_top_level(self, ts: str, text: str, room_id: str | None = None) -> None:
        """Edit a previously sent message via an m.replace relation.

        Per the Matrix spec, the visible body for legacy clients is
        prefixed with "* ", while m.new_content carries the replacement
        body for spec-aware clients. Both the visible and replacement
        bodies carry their own formatted HTML alongside the plain text.
        """
        target = self._room_or_default(room_id)
        rendered = _render_html(text)
        content = {
            "msgtype": "m.text",
            "body": f"* {text}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"* {rendered}",
            "m.new_content": {
                "msgtype": "m.text",
                "body": text,
                "format": "org.matrix.custom.html",
                "formatted_body": rendered,
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": ts},
        }
        await self._client.room_send(
            room_id=target,
            message_type="m.room.message",
            content=content,
        )

    async def react(self, ts: str, emoji: str, room_id: str | None = None) -> None:
        """Post an m.reaction annotation on event ts.

        Reactions are cosmetic confirmation; never let them fail a delivery.
        """
        target = self._room_or_default(room_id)
        key = _emoji_for(emoji)
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": ts,
                "key": key,
            }
        }
        try:
            await self._client.room_send(
                room_id=target,
                message_type="m.reaction",
                content=content,
            )
        except Exception as exc:
            log.warning("reaction %s on %s failed (non-fatal): %s", emoji, ts, exc)
