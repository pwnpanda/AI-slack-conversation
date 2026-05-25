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
        await self._zellij(session, "action", "focus-pane-id", pane_id)
        await self._zellij(session, "action", "write-chars", text)
        await self._zellij(session, "action", "write", "13")

    async def _zellij(self, session: str, *args: str) -> None:
        cmd = ["zellij", "--session", session, *args]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise ZellijError(
                f"zellij {' '.join(args)} -> {result.returncode}: {result.stderr.strip()}"
            )
        log.debug("zellij ok: %s", " ".join(args))
