import os
from pathlib import Path

import pytest

from slackbot.process_liveness import (
    _argv_contains,
    _argv_pair_contains,
    session_is_alive,
)


def _make_fake_proc(tmp_path: Path, pid: str, argv: list[str]) -> None:
    """Write argv to a fake /proc/<pid>/cmdline (NUL-separated, NUL-terminated)."""
    pdir = tmp_path / pid
    pdir.mkdir()
    (pdir / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")


def test_argv_contains_exact_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fake_proc(tmp_path, "100", ["claude", "--resume", "Finance"])
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    assert _argv_contains("Finance") is True
    assert _argv_contains("claude") is True
    # Substring must NOT match — Finance is a token, not a substring
    assert _argv_contains("inanc") is False


def test_argv_pair_contains_adjacent_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fake_proc(tmp_path, "200", ["claude", "--resume", "Finance"])
    _make_fake_proc(tmp_path, "201", ["claude", "Finance", "--resume"])  # wrong order
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    assert _argv_pair_contains("--resume", "Finance") is True
    # Reverse order should NOT match
    # pid 201 has Finance then --resume — that pair matches
    assert _argv_pair_contains("Finance", "--resume") is True
    # Non-existent pair
    assert _argv_pair_contains("--resume", "Marketing") is False


def test_argv_pair_ignores_substring_with_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell with a single arg containing '--resume Finance' (literal space) must NOT match."""
    _make_fake_proc(tmp_path, "300", ["bash", "-c", "echo --resume Finance"])
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    assert _argv_pair_contains("--resume", "Finance") is False


def test_session_is_alive_via_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = "f6b233bc-3110-49bd-ba6e-57d49459c522"
    _make_fake_proc(tmp_path, "400", ["claude", "--resume", sid])
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    assert session_is_alive(sid, None, None) is True


def test_session_is_alive_via_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fake_proc(tmp_path, "500", ["claude", "--resume", "babydev"])
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    # session_id not in cmdline, but name is — alive
    assert session_is_alive("some-uuid", None, "babydev") is True


def test_session_is_alive_falls_back_to_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    # No matching cmdline; cc_pid is our own process — must be alive
    assert session_is_alive("u", os.getpid(), "n") is True
    # Dead pid (very large, won't exist on Linux)
    assert session_is_alive("u", 99999999, "n") is False


def test_session_is_alive_unknown_assumes_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    # No session_id, no name, no pid — return True to avoid false negatives on legacy rows
    assert session_is_alive(None, None, None) is True
