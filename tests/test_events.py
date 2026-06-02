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


def test_format_prompt_multi_line_renders_markdown() -> None:
    """Multi-line prompt/response text is appended after a blank line so
    Element renders its markdown structure (headings, lists, fenced code
    blocks already in CC's output) natively — no outer code-block wrap,
    which would render the whole reply as a monospaced raw-text listing."""
    out = format_event("prompt", {"text": "line1\nline2", "agent": "codex"})
    assert out == "[Codex] 👤\n\nline1\nline2"
    assert "```" not in out


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


def test_format_notification_bare() -> None:
    assert format_event("notification", {"message": "approve?", "agent": "gemini"}) == (
        "[Gemini] ⏸ approve?"
    )


def test_format_notification_empty_message_falls_back() -> None:
    out = format_event("notification", {"agent": "claude"})
    assert out == "[Claude] ⏸ waiting for input"


def test_format_notification_with_tool_request_adds_hint() -> None:
    out = format_event(
        "notification",
        {
            "message": "Claude needs your permission",
            "agent": "claude",
            "tool_request": 'Bash({"cmd":"rm file"})',
        },
    )
    assert "⏸ Claude needs your permission" in out
    assert "Asking permission for:" in out
    assert "Bash({" in out
    assert "Reply `1`" in out


def test_format_notification_includes_context_tail() -> None:
    out = format_event(
        "notification",
        {
            "message": "needs input",
            "agent": "claude",
            "tool_request": "X(1)",
            "context": "line1\nline2\nline3\nline4\nline5\nline6\nline7",
        },
    )
    # Full context preserved — the old tail-truncation was lossy and surprised users.
    assert "line1" in out
    assert "line7" in out


def test_format_notification_no_tool_request_no_hint() -> None:
    out = format_event(
        "notification",
        {"message": "idle now", "agent": "claude"},
    )
    assert "Reply `1`" not in out
    assert "Asking permission" not in out


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


def test_long_multiline_response_is_not_truncated() -> None:
    """Replies must be preserved verbatim — no [truncated] tail. Chunking
    is a separate concern handled by chunk_for_matrix."""
    text = "\n".join(f"line {i}: " + ("x" * 60) for i in range(200))
    out = format_event("response", {"text": text, "agent": "claude"})
    assert "[truncated]" not in out
    assert "line 199:" in out
    assert text in out


def test_long_notification_context_is_not_truncated() -> None:
    """Notification context (tail of CC's pre-prompt output) must be
    preserved verbatim; the old 'last 6 lines + 3000 char cap' is gone."""
    ctx = "\n".join(f"output line {i}" for i in range(50))
    out = format_event(
        "notification",
        {"message": "Claude needs your permission", "context": ctx, "agent": "claude"},
    )
    assert "output line 0" in out
    assert "output line 49" in out


def test_chunk_for_matrix_returns_single_chunk_when_short() -> None:
    from slackbot.events import chunk_for_matrix

    assert chunk_for_matrix("hello") == ["hello"]
    big_but_under = "x" * 59_999
    assert chunk_for_matrix(big_but_under) == [big_but_under]


def test_chunk_for_matrix_splits_at_newline_boundary() -> None:
    from slackbot.events import chunk_for_matrix

    text = "\n".join(f"line {i}" for i in range(20000))  # well over 60k
    chunks = chunk_for_matrix(text)
    assert len(chunks) >= 2
    # Every chunk after the first is marked as a continuation.
    for c in chunks[1:]:
        assert c.startswith("…(part ")
    # Concatenation (stripping continuation markers) gets the original back.
    rejoined = chunks[0]
    for c in chunks[1:]:
        rejoined += "\n" + c.split("\n", 1)[1]
    assert rejoined == text


def test_chunk_for_matrix_hard_slices_when_line_too_long() -> None:
    from slackbot.events import chunk_for_matrix

    # A single 100k-char line with no newlines: must still be split, no
    # raise, no silent loss.
    text = "y" * 100_000
    chunks = chunk_for_matrix(text)
    assert len(chunks) >= 2
    # First chunk fits under the limit; total y count preserved.
    assert sum(c.count("y") for c in chunks) == 100_000


def test_parse_ask_user_question_extracts_first_question_and_options() -> None:
    from slackbot.events import parse_ask_user_question

    payload = (
        'AskUserQuestion({"questions":[{"question":"Which RGB?","header":"RGB",'
        '"multiSelect":false,"options":['
        '{"label":"Per-key","description":"SK6812"},'
        '{"label":"Underglow","description":""},'
        '{"label":"None"}'
        "]}]})"
    )
    q = parse_ask_user_question(payload)
    assert q is not None
    assert q["question"] == "Which RGB?"
    assert [o["label"] for o in q["options"]] == ["Per-key", "Underglow", "None"]
    assert q["options"][0]["description"] == "SK6812"
    assert q["options"][2]["description"] == ""


def test_parse_ask_user_question_returns_none_for_other_tools() -> None:
    from slackbot.events import parse_ask_user_question

    assert parse_ask_user_question('Bash({"command":"ls"})') is None
    assert parse_ask_user_question("") is None
    assert parse_ask_user_question("AskUserQuestion(not json)") is None
    assert parse_ask_user_question('AskUserQuestion({"questions":[]})') is None
    assert parse_ask_user_question('AskUserQuestion({"questions":[{}]})') is None


def test_notification_format_uses_question_display_when_ask_user_question() -> None:
    out = format_event(
        "notification",
        {
            "message": "Claude needs your permission",
            "tool_request": (
                'AskUserQuestion({"questions":[{"question":"Pick one",'
                '"options":[{"label":"Foo","description":""},'
                '{"label":"Bar","description":"second"}]}]})'
            ),
            "agent": "claude",
        },
    )
    # No 'approve/deny' template — wrong UI for AskUserQuestion.
    assert "Reply `1` to approve" not in out
    # Question and options shown.
    assert "❓ Pick one" in out
    assert "Foo" in out
    assert "Bar" in out
    assert "second" in out
