from slackbot.events import format_event, parse_rn_command, top_level_text


def test_top_level_text_active() -> None:
    assert top_level_text("myproj", "/home/r/x", "active") == "🟢 myproj  ·  /home/r/x"


def test_top_level_text_ended() -> None:
    assert top_level_text("myproj", "/x", "ended") == "⚪ myproj  ·  /x  (ended)"


def test_format_prompt_single_line() -> None:
    assert format_event("prompt", {"text": "hello"}) == "👤 hello"


def test_format_prompt_multi_line_codeblock() -> None:
    out = format_event("prompt", {"text": "line1\nline2"})
    assert out.startswith("👤\n```\n")
    assert "line1\nline2" in out


def test_format_response_with_tool_summary() -> None:
    out = format_event("response", {"text": "done", "tool_summary": "2 reads, 1 edit"})
    assert "🤖 done" in out
    assert "↳ 2 reads, 1 edit" in out


def test_format_response_no_summary() -> None:
    out = format_event("response", {"text": "ok"})
    assert "↳" not in out
    assert "🤖 ok" in out


def test_format_notification() -> None:
    assert format_event("notification", {"message": "approve?"}) == "⏸ approve?"


def test_format_error() -> None:
    assert format_event("error", {"text": "boom"}) == "❌ boom"


def test_format_unknown_kind_returns_repr() -> None:
    out = format_event("weird", {"x": 1})
    assert "weird" in out


def test_parse_rn_command_match() -> None:
    assert parse_rn_command("/rn slackbot-claude") == "slackbot-claude"
    assert parse_rn_command("/rn  my-name") == "my-name"
    assert parse_rn_command("/rename my-name") == "my-name"


def test_parse_rn_command_no_match() -> None:
    assert parse_rn_command("regular prompt") is None
    assert parse_rn_command("/rnx not-a-name") is None
    assert parse_rn_command("/rn") is None
