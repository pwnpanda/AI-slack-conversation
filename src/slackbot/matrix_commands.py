"""Matrix-side slash-style commands typed as top-level room messages.

Currently supports `/new <name>` which spawns a new CC pane in zellij and
types `/rn <name>` into it after a short delay. CC's own session_start +
/rn handling then creates the Matrix top-level message and binds the thread.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from slackbot.registry import Registry

log = logging.getLogger(__name__)

_NEW_PATTERN = re.compile(r"^/new\s+(\S+)\s*$")


class _MatrixIOProto(Protocol):
    async def post_in_thread(
        self, thread_root: str, text: str, room_id: str | None = None
    ) -> str: ...
    async def react(self, ts: str, emoji: str, room_id: str | None = None) -> None: ...


class _ActuatorProto(Protocol):
    async def spawn_pane_with_command(
        self,
        session: str,
        command_argv: tuple[str, ...] | list[str],
        initial_text: str,
        delay_seconds: float,
    ) -> None: ...


class MatrixCommandHandler:
    """Dispatch top-level Matrix messages that start with a slash command."""

    def __init__(
        self,
        reg: Registry,
        matrix: _MatrixIOProto,
        actuator: _ActuatorProto,
        zellij_session: str,
        new_pane_command: tuple[str, ...],
        new_pane_delay_seconds: float,
    ) -> None:
        self._reg = reg
        self._matrix = matrix
        self._actuator = actuator
        self._zellij_session = zellij_session
        self._new_pane_command = new_pane_command
        self._new_pane_delay_seconds = new_pane_delay_seconds

    async def maybe_handle(self, room_id: str, text: str, msg_ts: str) -> bool:
        """Return True if *text* matched a command and was handled."""
        m = _NEW_PATTERN.match(text.strip())
        if not m:
            return False
        await self._handle_new(room_id=room_id, name=m.group(1), msg_ts=msg_ts)
        return True

    async def _handle_new(self, room_id: str, name: str, msg_ts: str) -> None:
        existing = self._reg.get_session_by_name(name, room_id=room_id)
        if existing is not None:
            await self._matrix.react(msg_ts, "x", room_id=room_id)
            await self._matrix.post_in_thread(
                msg_ts,
                f"❌ Name `{name}` already in use by session "
                f"`{existing.cc_session_id}` (status: {existing.status}). "
                f"Pick a different name or rename the existing one.",
                room_id=room_id,
            )
            return
        await self._matrix.react(msg_ts, "hourglass_flowing_sand", room_id=room_id)
        try:
            await self._actuator.spawn_pane_with_command(
                session=self._zellij_session,
                command_argv=self._new_pane_command,
                initial_text=f"/rn {name}",
                delay_seconds=self._new_pane_delay_seconds,
            )
        except Exception as exc:
            log.exception("spawn_pane_with_command failed for /new %s", name)
            await self._matrix.post_in_thread(
                msg_ts,
                f"❌ Failed to spawn new pane: {exc}",
                room_id=room_id,
            )
            return
        await self._matrix.react(msg_ts, "white_check_mark", room_id=room_id)
