"""Shared pytest fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def env_full(monkeypatch: pytest.MonkeyPatch, tmp_db_path: str) -> None:
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.com")
    monkeypatch.setenv("MATRIX_USER_ID", "@ai-bot:matrix.example.com")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "syt_test_token")
    monkeypatch.setenv("MATRIX_DEVICE_ID", "slackbot-test")
    monkeypatch.setenv("MATRIX_ROOM_ID", "!default:matrix.example.com")
    monkeypatch.setenv("SLACKBOT_PORT", "0")
    monkeypatch.setenv("SLACKBOT_DB_PATH", tmp_db_path)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
