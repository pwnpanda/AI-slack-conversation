"""Drives the host's zellij multiplexer via subprocess calls (no shell)."""

from __future__ import annotations

import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)


class ZellijError(RuntimeError):
    pass


class ZellijActuator:
    async def deliver(self, session: str, pane_id: str, text: str) -> None:
        await self._zellij(session, "action", "focus-pane-id", pane_id, allow_already=True)
        await self._zellij(session, "action", "write-chars", text)
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
