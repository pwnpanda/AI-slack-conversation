"""Environment-driven configuration."""

from __future__ import annotations

import os
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
    tmp_dir: str
    verbose: str
    log_level: str
    stale_after_seconds: int

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


def load_config() -> Config:
    return Config(
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_app_token=_require("SLACK_APP_TOKEN"),
        slack_channel_id=_require("SLACK_CHANNEL_ID"),
        agent_channels=_agent_channels(),
        port=int(os.environ.get("SLACKBOT_PORT", "8787")),
        db_path=os.environ.get("SLACKBOT_DB_PATH") or _default_db_path(),
        tmp_dir=os.environ.get("SLACKBOT_TMP_DIR", "/tmp"),
        verbose=os.environ.get("CC_SLACK_VERBOSE", "off"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        # 6h default: a session that hasn't fired any hook in 6h is likely dead
        # (panes killed, zellij restarted, etc.) — replies would misroute.
        stale_after_seconds=int(os.environ.get("SLACKBOT_STALE_AFTER_SECONDS", "21600")),
    )
