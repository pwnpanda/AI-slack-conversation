"""Shared pytest fixtures."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def env_full(monkeypatch: pytest.MonkeyPatch, tmp_db_path: str) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C12345")
    monkeypatch.setenv("SLACKBOT_PORT", "0")
    monkeypatch.setenv("SLACKBOT_DB_PATH", tmp_db_path)
    monkeypatch.setenv("SLACKBOT_TMP_DIR", tempfile.gettempdir())
    monkeypatch.setenv("CC_SLACK_VERBOSE", "off")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
