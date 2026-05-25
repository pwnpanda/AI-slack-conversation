from slackbot.events import format_event, parse_rn_command, top_level_text


def test_top_level_text_active() -> None:
    assert top_level_text("myproj", "/home/r/x", "active", "codex") == (
        "🟢 [Codex] myproj  ·  /home/r/x"
    )


def test_top_level_text_ended() -> None:
    assert top_level_text("myproj", "/x", "ended", "gemini") == (
        "⚪ [Gemini] myproj  ·  /x  (ended)"
    )


def test_format_prompt_single_line() -> None:
    assert format_event("prompt", {"text": "hello", "agent": "claude"}) == "[Claude] 👤 hello"


def test_format_prompt_multi_line_codeblock() -> None:
    out = format_event("prompt", {"text": "line1\nline2", "agent": "codex"})
    assert out.startswith("[Codex] 👤\n```\n")
    assert "line1\nline2" in out


def test_format_response_with_tool_summary() -> None:
    out = format_event(
        "response",
        {"text": "done", "tool_summary": "2 reads, 1 edit", "agent": "gemini"},
    )
    assert "[Gemini] 🤖 done" in out
    assert "↳ 2 reads, 1 edit" in out


def test_format_response_no_summary() -> None:
    out = format_event("response", {"text": "ok", "agent": "codex"})
    assert "↳" not in out
    assert "[Codex] 🤖 ok" in out


def test_format_notification() -> None:
    assert format_event("notification", {"message": "approve?", "agent": "gemini"}) == (
        "[Gemini] ⏸ approve?"
    )


def test_format_error() -> None:
    assert format_event("error", {"text": "boom", "agent": "claude"}) == "[Claude] ❌ boom"


def test_format_unknown_kind_returns_repr() -> None:
    out = format_event("weird", {"x": 1, "agent": "codex"})
    assert out.startswith("[Codex] ")
    assert "weird" in out


def test_parse_rn_command_match() -> None:
    assert parse_rn_command("/rn slackbot-claude") == "slackbot-claude"
    assert parse_rn_command("/rn  my-name") == "my-name"
    assert parse_rn_command("/rename my-name") == "my-name"
    assert parse_rn_command("rn codex-name") == "codex-name"
    assert parse_rn_command("#rn gemini-name") == "gemini-name"
    assert parse_rn_command("!register session-name") == "session-name"


def test_parse_rn_command_no_match() -> None:
    assert parse_rn_command("regular prompt") is None
    assert parse_rn_command("/rnx not-a-name") is None
    assert parse_rn_command("/rn") is None
    assert parse_rn_command("rename natural-language-prompt") is None
    assert parse_rn_command("register natural-language-prompt") is None
