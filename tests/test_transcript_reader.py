import json
from pathlib import Path

from slackbot.transcript_reader import TranscriptReader


def _append(path: Path, *records: dict) -> None:
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_reader_emits_user_and_assistant(tmp_path: Path) -> None:
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()

    _append(
        p,
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hello back"}]},
        },
    )
    events = list(reader.drain())
    assert events == [
        {"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "hi"},
        {"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hello back"},
    ]
    reader.close()


def test_reader_handles_partial_line(tmp_path: Path) -> None:
    """A line written without trailing newline is held back until the newline arrives."""
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    with p.open("a") as f:
        f.write('{"type":"user","uuid":"u1","parentUuid":null,')
        f.flush()
    assert list(reader.drain()) == []
    with p.open("a") as f:
        f.write('"message":{"role":"user","content":"hi"}}\n')
        f.flush()
    events = list(reader.drain())
    assert len(events) == 1
    assert events[0]["uuid"] == "u1"
    reader.close()


def test_reader_skips_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": "ok"},
        },
    )
    with p.open("a") as f:
        f.write("not json at all\n")
    _append(
        p,
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": None,
            "message": {"role": "user", "content": "ok2"},
        },
    )
    events = list(reader.drain())
    assert [e["uuid"] for e in events] == ["u1", "u2"]
    reader.close()


def test_reader_ignores_non_text_assistant_content(tmp_path: Path) -> None:
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Read", "input": {}}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        },
    )
    events = list(reader.drain())
    # a1 has no text content → emitted with empty string and we choose to skip
    assert events == [{"kind": "response", "uuid": "a2", "parentUuid": "u1", "text": "done"}]
    reader.close()


def test_reader_attaches_tool_summary_when_text_and_tool_use_mixed(tmp_path: Path) -> None:
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me look"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
    )
    events = list(reader.drain())
    assert events == [
        {
            "kind": "response",
            "uuid": "a1",
            "parentUuid": "u1",
            "text": "Let me look",
            "tool_summary": "Read, Bash",
        }
    ]
    reader.close()


def test_reader_survives_file_truncation(tmp_path: Path) -> None:
    """If the file shrinks (rotation), the reader resets to offset 0."""
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": "old"},
        },
    )
    list(reader.drain())  # consume
    # Truncate and write new content
    p.write_text("")
    _append(
        p,
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": None,
            "message": {"role": "user", "content": "new"},
        },
    )
    events = list(reader.drain())
    assert [e["uuid"] for e in events] == ["u2"]
    reader.close()


def test_reader_with_start_offset_reads_from_there(tmp_path: Path) -> None:
    """A reader constructed with start_offset resumes from that byte position
    instead of snapping to EOF, so events written before the reader opened are
    still delivered."""
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    _append(
        p,
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": "first"},
        },
    )
    offset_before_second = p.stat().st_size
    _append(
        p,
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": None,
            "message": {"role": "user", "content": "second"},
        },
    )

    reader = TranscriptReader(p, start_offset=offset_before_second)
    reader.open()
    events = list(reader.drain())
    assert [e["uuid"] for e in events] == ["u2"]
    reader.close()


def test_reader_offset_round_trip_preserves_position(tmp_path: Path) -> None:
    """Persisting reader.offset and re-opening with start_offset resumes at
    the same point — no duplicate events, no dropped events."""
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": "first"},
        },
    )
    list(reader.drain())
    saved_offset = reader.offset
    reader.close()

    # Simulate daemon down: append another event while no reader is attached.
    _append(
        p,
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": None,
            "message": {"role": "user", "content": "second"},
        },
    )

    # Re-open from the saved offset; u1 must not be re-delivered, u2 must arrive.
    resumed = TranscriptReader(p, start_offset=saved_offset)
    resumed.open()
    events = list(resumed.drain())
    assert [e["uuid"] for e in events] == ["u2"]
    resumed.close()


def test_reader_handles_string_content_legacy(tmp_path: Path) -> None:
    """Older transcripts stored content as plain string instead of array of blocks."""
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "message": {"role": "assistant", "content": "legacy text"},
        },
    )
    events = list(reader.drain())
    assert events == [{"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "legacy text"}]
    reader.close()


def test_reader_emits_codex_user_and_assistant_messages(tmp_path: Path) -> None:
    p = tmp_path / "rollout.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "u1",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "a1",
                "role": "assistant",
                "phase": "final",
                "content": [{"type": "output_text", "text": "hello back"}],
            },
        },
    )
    assert list(reader.drain()) == [
        {"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "hello"},
        {"kind": "response", "uuid": "a1", "parentUuid": None, "text": "hello back"},
    ]
    reader.close()


def test_reader_ignores_codex_event_message_duplicate(tmp_path: Path) -> None:
    p = tmp_path / "rollout.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(
        p,
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "duplicate"}},
    )
    assert list(reader.drain()) == []
    reader.close()
