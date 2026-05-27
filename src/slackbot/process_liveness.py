"""Liveness check for a CC session.

A naive `os.kill(pid, 0)` is wrong here: tools like `claude-auto-resume.sh`
restart the CC process under the SAME session id, so the pid recorded at the
last hook fire is almost always *not* the pid that's running right now. We need
an identifier stable across restarts — the session_id or the registered name.

/proc/PID/cmdline stores argv NUL-separated. Substring matching is unreliable
because (a) a NUL boundary breaks "--resume Finance" with a literal space, and
(b) shell wrappers may contain that literal as a single-arg string. We split
on NUL and match by exact argv tokens.
"""

from __future__ import annotations

import os
from pathlib import Path

# Indirected so tests can substitute a tmp dir.
_PROC = Path("/proc")


def _iter_cmdlines() -> list[list[bytes]]:
    """Return parsed argv for each running process under /proc."""
    results: list[list[bytes]] = []
    if not _PROC.is_dir():
        return results
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
        if not raw:
            continue
        # cmdline is NUL-separated and NUL-terminated; split and drop trailing empty.
        argv = raw.split(b"\x00")
        if argv and argv[-1] == b"":
            argv = argv[:-1]
        results.append(argv)
    return results


def _argv_contains(token: str) -> bool:
    needle = token.encode("utf-8", errors="ignore")
    return any(needle in argv for argv in _iter_cmdlines())


def _argv_pair_contains(first: str, second: str) -> bool:
    first_b = first.encode("utf-8", errors="ignore")
    second_b = second.encode("utf-8", errors="ignore")
    for argv in _iter_cmdlines():
        for i in range(len(argv) - 1):
            if argv[i] == first_b and argv[i + 1] == second_b:
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
    except OverflowError:
        return False
    return True


def session_is_alive(
    cc_session_id: str | None,
    cc_pid: int | None,
    name: str | None,
) -> bool:
    """Return True if a CC process for this session is running.

    Probes, in order:
      1. session_id as a standalone argv token in any /proc/*/cmdline.
      2. `--resume <name>` as adjacent argv tokens.
      3. kill -0 on the recorded cc_pid (fresh, never-resumed sessions).
    Returns True with no info, so legacy rows still deliver.
    """
    if cc_session_id and _argv_contains(cc_session_id):
        return True
    if name and _argv_pair_contains("--resume", name):
        return True
    if cc_pid is not None and cc_pid > 0:
        return pid_is_alive(cc_pid)
    return True
