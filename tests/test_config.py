import pytest

from slackbot.config import load_config


def test_load_config_full_env(env_full: None) -> None:
    cfg = load_config()
    assert cfg.matrix_homeserver == "https://matrix.example.com"
    assert cfg.matrix_user_id == "@ai-bot:matrix.example.com"
    assert cfg.matrix_access_token == "syt_test_token"
    assert cfg.matrix_device_id == "slackbot-test"
    assert cfg.matrix_default_room == "!default:matrix.example.com"
    assert cfg.room_for_agent("claude") == "!default:matrix.example.com"
    assert cfg.port == 0
    assert cfg.log_level == "WARNING"
    assert cfg.local_llm.enabled is False


def test_load_config_agent_room_overrides(env_full: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRIX_ROOM_ID_CODEX", "!codex:matrix.example.com")
    monkeypatch.setenv("MATRIX_ROOM_ID_GEMINI", "!gemini:matrix.example.com")
    cfg = load_config()
    assert cfg.room_for_agent("codex") == "!codex:matrix.example.com"
    assert cfg.room_for_agent("gemini") == "!gemini:matrix.example.com"
    assert cfg.room_for_agent("unknown") == "!default:matrix.example.com"


def test_load_config_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MATRIX_HOMESERVER",
        "MATRIX_USER_ID",
        "MATRIX_ACCESS_TOKEN",
        "MATRIX_DEVICE_ID",
        "MATRIX_ROOM_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="MATRIX_HOMESERVER"):
        load_config()


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.com")
    monkeypatch.setenv("MATRIX_USER_ID", "@ai-bot:matrix.example.com")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "x")
    monkeypatch.setenv("MATRIX_DEVICE_ID", "x")
    monkeypatch.setenv("MATRIX_ROOM_ID", "!default:matrix.example.com")
    for var in ("SLACKBOT_PORT", "LOG_LEVEL", "SLACKBOT_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.port == 8787
    assert cfg.log_level == "INFO"
    assert cfg.db_path.endswith("/claude-slack-bot/registry.db")
    assert cfg.local_llm.model == "qwen3.5-4b-local"
    assert cfg.local_llm.idle_warning_seconds == 1800
    assert cfg.local_llm.idle_grace_seconds == 180
    assert cfg.local_llm.web_proxy_port == 9321


def test_load_config_enables_local_model(env_full: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:9322/v1")
    monkeypatch.setenv("LLAMA_API_KEY", "local-secret")

    cfg = load_config()

    assert cfg.local_llm.enabled is True
    assert cfg.local_llm.base_url == "http://127.0.0.1:9322/v1"
    assert cfg.local_llm.api_key == "local-secret"
