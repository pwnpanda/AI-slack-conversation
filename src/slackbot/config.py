"""Environment-driven configuration."""

from __future__ import annotations

import os
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    matrix_homeserver: str
    matrix_user_id: str
    matrix_access_token: str
    matrix_device_id: str
    matrix_default_room: str
    agent_rooms: dict[str, str]
    port: int
    db_path: str
    log_level: str
    # Per-host routing: when true (default) the daemon resolves a single
    # Matrix room named after this machine's hostname (creating it if
    # absent) and routes ALL providers on this host into it — so work vs
    # private is separated by machine, and adding/removing a daemon host
    # needs no manual room wiring. When true, agent_rooms is ignored.
    room_by_hostname: bool = True
    hostname: str = ""
    # Optional second account for posting CC-typed user prompts under the
    # human's identity instead of the bot's. When both env vars are set,
    # the bot logs in twice and routes 👤 mirrors through this account so
    # Element shows them as the user's own messages.
    matrix_user_user_id: str | None = None
    matrix_user_access_token: str | None = None
    # /new <name> spawns a CC pane in this zellij session and types "/rn <name>"
    # into it after a short delay. Override via env when zellij session differs.
    new_pane_zellij_session: str = "ai"
    new_pane_command: tuple[str, ...] = ("claude", "--dangerously-skip-permissions")
    new_pane_delay_seconds: float = 5.0

    def room_for_agent(self, agent: str) -> str:
        return self.agent_rooms.get(agent.lower(), self.matrix_default_room)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _default_db_path() -> str:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(state) / "claude-slack-bot" / "registry.db")


def _agent_rooms() -> dict[str, str]:
    rooms: dict[str, str] = {}
    for agent in ("claude", "codex", "gemini"):
        room = os.environ.get(f"MATRIX_ROOM_ID_{agent.upper()}")
        if room:
            rooms[agent] = room
    return rooms


def _new_pane_command() -> tuple[str, ...]:
    raw = os.environ.get("SLACKBOT_NEW_PANE_COMMAND")
    if not raw:
        return ("claude", "--dangerously-skip-permissions")
    return tuple(shlex.split(raw))


def load_config() -> Config:
    return Config(
        matrix_homeserver=_require("MATRIX_HOMESERVER"),
        matrix_user_id=_require("MATRIX_USER_ID"),
        matrix_access_token=_require("MATRIX_ACCESS_TOKEN"),
        matrix_device_id=_require("MATRIX_DEVICE_ID"),
        matrix_default_room=_require("MATRIX_ROOM_ID"),
        agent_rooms=_agent_rooms(),
        port=int(os.environ.get("SLACKBOT_PORT", "8787")),
        db_path=os.environ.get("SLACKBOT_DB_PATH") or _default_db_path(),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        new_pane_zellij_session=os.environ.get("SLACKBOT_NEW_PANE_ZELLIJ_SESSION", "ai"),
        new_pane_command=_new_pane_command(),
        new_pane_delay_seconds=float(os.environ.get("SLACKBOT_NEW_PANE_DELAY_SECONDS", "5")),
        matrix_user_user_id=os.environ.get("MATRIX_USER_USER_ID") or None,
        matrix_user_access_token=os.environ.get("MATRIX_USER_ACCESS_TOKEN") or None,
        room_by_hostname=os.environ.get("SLACKBOT_ROOM_BY_HOSTNAME", "1") not in ("0", "false", ""),
        hostname=os.environ.get("SLACKBOT_HOSTNAME") or socket.gethostname(),
    )
