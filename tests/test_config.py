import pytest

from slackbot.config import load_config


def test_load_config_full_env(env_full: None) -> None:
    cfg = load_config()
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.slack_app_token == "xapp-test"
    assert cfg.slack_channel_id == "C12345"
    assert cfg.channel_for_agent("claude") == "C12345"
    assert cfg.port == 0
    assert cfg.verbose == "off"
    assert cfg.log_level == "WARNING"


def test_load_config_agent_channel_overrides(
    env_full: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_ID_CODEX", "C-CODEX")
    monkeypatch.setenv("SLACK_CHANNEL_ID_GEMINI", "C-GEMINI")
    cfg = load_config()
    assert cfg.channel_for_agent("codex") == "C-CODEX"
    assert cfg.channel_for_agent("gemini") == "C-GEMINI"
    assert cfg.channel_for_agent("unknown") == "C12345"


def test_load_config_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        load_config()


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "x")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "x")
    for var in ("SLACKBOT_PORT", "CC_SLACK_VERBOSE", "LOG_LEVEL", "SLACKBOT_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.port == 8787
    assert cfg.verbose == "off"
    assert cfg.log_level == "INFO"
    assert cfg.db_path.endswith("/claude-slack-bot/registry.db")
