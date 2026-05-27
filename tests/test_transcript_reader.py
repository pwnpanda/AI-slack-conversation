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
