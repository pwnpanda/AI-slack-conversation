"""Matrix-side slash-style commands typed as top-level room messages.

`/new <name>` — spawn a fresh CC pane in zellij, type `/rn <name>` into it.
`/resume <name>` — spawn a CC pane resuming the named session (`claude --resume <name>`).
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from slackbot.registry import Registry

log = logging.getLogger(__name__)

_NEW_PATTERN = re.compile(r"^/new\s+(\S+)\s*$")
_RESUME_PATTERN = re.compile(r"^/resume\s+(\S+)\s*$")


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
        stripped = text.strip()
        m = _NEW_PATTERN.match(stripped)
        if m:
            await self._handle_new(room_id=room_id, name=m.group(1), msg_ts=msg_ts)
            return True
        m = _RESUME_PATTERN.match(stripped)
        if m:
            await self._handle_resume(room_id=room_id, name=m.group(1), msg_ts=msg_ts)
            return True
        return False

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

    async def _handle_resume(self, room_id: str, name: str, msg_ts: str) -> None:
        """Spawn a CC pane with `claude --resume <name>`.

        Unlike /new, no /rn is typed afterwards — CC's own SessionStart
        hook fires with the resumed session_id, and the existing registry
        row (if any) is found by sid, so the Matrix thread re-binds
        automatically. If the session name isn't already in the registry,
        CC will still resume by name via the user's shell wrapper
        (auto-resume.sh resolves names to ids); the bot just won't have
        a prior thread to attach to until /rn fires.
        """
        await self._matrix.react(msg_ts, "hourglass_flowing_sand", room_id=room_id)
        try:
            await self._actuator.spawn_pane_with_command(
                session=self._zellij_session,
                command_argv=(*self._new_pane_command, "--resume", name),
                initial_text="",
                delay_seconds=self._new_pane_delay_seconds,
            )
        except Exception as exc:
            log.exception("spawn_pane_with_command failed for /resume %s", name)
            await self._matrix.post_in_thread(
                msg_ts,
                f"❌ Failed to spawn resume pane for `{name}`: {exc}",
                room_id=room_id,
            )
            return
        await self._matrix.react(msg_ts, "white_check_mark", room_id=room_id)
