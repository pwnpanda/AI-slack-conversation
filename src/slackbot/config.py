"""Environment-driven configuration."""

from __future__ import annotations

import os
import shlex
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
    )
