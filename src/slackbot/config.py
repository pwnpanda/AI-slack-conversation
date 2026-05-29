"""Environment-driven configuration."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    slack_channel_id: str
    agent_channels: dict[str, str]
    port: int
    db_path: str
    log_level: str
    # /new <name> spawns a CC pane in this zellij session and types "/rn <name>"
    # into it after a short delay. Override via env when zellij session differs.
    new_pane_zellij_session: str = "main"
    new_pane_command: tuple[str, ...] = ("claude", "--dangerously-skip-permissions")
    new_pane_delay_seconds: float = 5.0

    def channel_for_agent(self, agent: str) -> str:
        return self.agent_channels.get(agent.lower(), self.slack_channel_id)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _default_db_path() -> str:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(state) / "claude-slack-bot" / "registry.db")


def _agent_channels() -> dict[str, str]:
    channels = {}
    for agent in ("claude", "codex", "gemini"):
        channel = os.environ.get(f"SLACK_CHANNEL_ID_{agent.upper()}")
        if channel:
            channels[agent] = channel
    return channels


def _new_pane_command() -> tuple[str, ...]:
    raw = os.environ.get("SLACKBOT_NEW_PANE_COMMAND")
    if not raw:
        return ("claude", "--dangerously-skip-permissions")
    return tuple(shlex.split(raw))


def load_config() -> Config:
    return Config(
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_app_token=_require("SLACK_APP_TOKEN"),
        slack_channel_id=_require("SLACK_CHANNEL_ID"),
        agent_channels=_agent_channels(),
        port=int(os.environ.get("SLACKBOT_PORT", "8787")),
        db_path=os.environ.get("SLACKBOT_DB_PATH") or _default_db_path(),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        new_pane_zellij_session=os.environ.get("SLACKBOT_NEW_PANE_ZELLIJ_SESSION", "main"),
        new_pane_command=_new_pane_command(),
        new_pane_delay_seconds=float(os.environ.get("SLACKBOT_NEW_PANE_DELAY_SECONDS", "5")),
    )
