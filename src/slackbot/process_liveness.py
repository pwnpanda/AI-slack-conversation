"""Decide whether a Claude/Codex/Gemini CC session is still running.

A naive `os.kill(pid, 0)` is wrong here: tools like `claude-auto-resume.sh`
restart the CC process under the SAME session id, so the pid recorded at the
last hook fire is almost always *not* the pid that's running right now. We need
an identifier stable across restarts — the session_id, which always appears in
the resumed CC's command line (`claude --resume <session_id>`).

`session_is_alive` scans /proc for any process whose cmdline contains the
session_id. Falls back to the recorded pid for the rare case where session_id
isn't in cmdline (e.g. a fresh CC that hasn't been resumed). Returns True for
unknown/legacy rows so we never refuse delivery without positive evidence of
death.
"""

from __future__ import annotations

import os
from pathlib import Path


def _has_process_with_cmdline_containing(needle: str) -> bool:
    proc = Path("/proc")
    if not needle or not proc.is_dir():
        return False
    needle_bytes = needle.encode("utf-8", errors="ignore")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
        if needle_bytes in cmdline:
            return True
    return False


def pid_is_alive(pid: int | None) -> bool:
    """Return True if `pid` is a running process under our uid, False if not."""
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def session_is_alive(cc_session_id: str | None, cc_pid: int | None) -> bool:
    """Return True if a CC process for `cc_session_id` is running.

    Stable across CC restarts that reuse the session id (the common
    auto-resume pattern). Falls back to pid only when session_id isn't found
    in any cmdline, and returns True when we have no info at all so legacy
    rows continue to deliver.
    """
    if cc_session_id and _has_process_with_cmdline_containing(cc_session_id):
        return True
    if cc_pid is not None and cc_pid > 0:
        return pid_is_alive(cc_pid)
    return True  # no evidence either way — assume alive
