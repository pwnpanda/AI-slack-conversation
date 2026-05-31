"""Drives the host's zellij multiplexer via subprocess calls (no shell)."""

from __future__ import annotations

import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)


class ZellijError(RuntimeError):
    pass


class ZellijActuator:
    def __init__(self) -> None:
        # Serialize deliveries: focus + write-chars + Enter are three separate
        # zellij calls. With concurrent deliveries to different panes, a second
        # delivery's `focus` could land between the first delivery's `focus` and
        # `write-chars`, sending the first reply's text into the second pane.
        # The lock keeps each three-call sequence atomic.
        self._lock = asyncio.Lock()

    async def deliver(self, session: str, pane_id: str, text: str) -> None:
        async with self._lock:
            await self._zellij(session, "action", "focus-pane-id", pane_id, allow_already=True)
            await self._zellij(session, "action", "write-chars", text)
            await self._zellij(session, "action", "write", "13")

    async def deliver_keys(self, session: str, pane_id: str, keys: list[str]) -> None:
        """Focus the pane and send a sequence of named keys (Down, Enter, …).

        Used for navigating TUI option lists (e.g. CC's AskUserQuestion)
        where typing the literal option number would land in the "Other"
        free-text field instead of selecting the option.
        """
        if not keys:
            return
        async with self._lock:
            await self._zellij(session, "action", "focus-pane-id", pane_id, allow_already=True)
            await self._zellij(session, "action", "send-keys", *keys)

    async def spawn_pane_with_command(
        self,
        session: str,
        command_argv: tuple[str, ...] | list[str],
        initial_text: str,
        delay_seconds: float,
    ) -> None:
        """Open a new pane running *command_argv*, then type *initial_text* + Enter.

        Holds the lock through the whole sequence so other deliveries can't
        steal focus between the new-pane and the write-chars. The sleep
        blocks other zellij actuations for delay_seconds — acceptable at
        single-user scale; tune via SLACKBOT_NEW_PANE_DELAY_SECONDS.
        """
        if not command_argv:
            raise ZellijError("spawn_pane_with_command requires a non-empty command")
        async with self._lock:
            await self._zellij(session, "action", "new-pane", "--", *command_argv)
            if initial_text:
                await asyncio.sleep(delay_seconds)
                await self._zellij(session, "action", "write-chars", initial_text)
                await self._zellij(session, "action", "write", "13")

    async def _zellij(self, session: str, *args: str, allow_already: bool = False) -> None:
        cmd = ["zellij", "--session", session, *args]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr = stderr.strip()
            # zellij exits non-zero with "already focused" - that's the desired
            # state for us, not a failure.
            if allow_already and "already focused" in stderr:
                log.debug("zellij focus no-op (pane already focused): %s", " ".join(args))
                return
            raise ZellijError(f"zellij {' '.join(args)} -> {result.returncode}: {stderr}")
        log.debug("zellij ok: %s", " ".join(args))
