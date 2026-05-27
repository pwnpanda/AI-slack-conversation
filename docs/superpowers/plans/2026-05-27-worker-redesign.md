# Worker-Per-Conversation Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current single-actuator + hook-side transcript polling architecture with a per-conversation worker model that consumes Claude Code's transcript JSONL via inotify and correlates responses to prompts by `parentUuid` — eliminating the wrong-turn race, the two-chat focus race, and stale-pid false deaths.

**Architecture:** Per-`cc_session_id` `Worker` (asyncio Task + Queue) owns hot state (`posted_uuids`, prompt uuid); a `Supervisor` spawns/reaps workers; a `TranscriptReader` watches each CC session's JSONL with inotify and enqueues parsed events into its worker; the registry remains the durable single-writer truth; one global `ZellijActuator` lock keeps the 3-call zellij sequence atomic across workers.

**Tech Stack:** Python 3.13, `aiohttp`, `slack-bolt` (Socket Mode), `slack-sdk`, stdlib `sqlite3`, stdlib `os.read` against `inotify_init1` via the `inotify_simple` package, stdlib `pytest` + `pytest-asyncio`, systemd `Type=notify` + `WatchdogSec=`.

**Spec:** [`docs/superpowers/specs/2026-05-27-worker-redesign.md`](../specs/2026-05-27-worker-redesign.md)

**Working directory:** `~/git/priv/claude-slack-bot/`

---

## File Map

| Path | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | add `inotify_simple` |
| `src/slackbot/process_liveness.py` | rewrite | argv-token matcher (split on `\x00`, compare exact tokens) |
| `src/slackbot/liveness_cache.py` | create | 10s TTL wrapper around `session_is_alive`, runs scan in `asyncio.to_thread` |
| `src/slackbot/transcript_reader.py` | create | per-session inotify watcher over JSONL; emits parsed prompt/response events |
| `src/slackbot/worker.py` | create | per-session async loop: queue, `posted_uuids`, `prompt_uuid`, echo-suppression, calls actuator+slack |
| `src/slackbot/supervisor.py` | create | `dict[sid, Worker]`, spawn on first event, reap after idle |
| `src/slackbot/sd_notify.py` | create | thin wrapper over `$NOTIFY_SOCKET` for `WATCHDOG=1` / `READY=1` |
| `src/slackbot/registry.py` | modify | (a) `transcript_path` column added; (b) `refresh_liveness` drops `status='active'`; (c) `claim_name` wrapped in `BEGIN IMMEDIATE`; (d) `find_recoverable_session` drops time clause |
| `src/slackbot/handlers.py` | modify | dispatch ALL events into supervisor; auto-create row on any event |
| `src/slackbot/reply_router.py` | modify | enqueue Slack reply into worker; no direct actuator call |
| `src/slackbot/__main__.py` | modify | wire supervisor + watchdog; replace `is_ping_pong_failing` with inactivity timer |
| `src/slackbot/dedupe.py` | delete | replaced by per-worker `posted_uuids` (and per-worker echo set) |
| `hooks/session_start.sh` | modify | include `transcript_path` from stdin |
| `systemd/claude-slack-bot.service` | modify | `Type=notify`, `WatchdogSec=600` |
| `tests/test_process_liveness.py` | create | argv-token matcher correctness |
| `tests/test_liveness_cache.py` | create | TTL + dedupe behaviour |
| `tests/test_transcript_reader.py` | create | parse, partial-line, rotation, malformed |
| `tests/test_worker.py` | create | queue draining, uuid dedupe, echo suppression |
| `tests/test_supervisor.py` | create | spawn-on-event, reap-on-idle |
| `tests/test_registry.py` | modify | add tests for claim_name transactional, refresh_liveness no longer flips, transcript_path roundtrip |
| `tests/test_integration.py` | create | end-to-end: fake transcript → worker → fake slack |

Order of work is bottom-up: foundations (liveness, transcript reader) → orchestration (worker, supervisor) → integration (handlers, reply_router, main) → infra (systemd, hooks) → cleanup.

---

## Task 1: Add inotify_simple dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dep**

Edit `pyproject.toml`, under `[project].dependencies`, add:

```toml
  "inotify_simple==1.3.5",
```

- [ ] **Step 2: Install**

```bash
uv sync --all-groups
```

Expected: `inotify_simple==1.3.5` installed.

- [ ] **Step 3: Verify import**

```bash
uv run python -c "from inotify_simple import INotify, flags; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add inotify_simple for transcript tailing"
```

---

## Task 2: Argv-token-based liveness matcher

**Files:**
- Modify: `src/slackbot/process_liveness.py`
- Create: `tests/test_process_liveness.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_process_liveness.py`:

```python
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
    assert _argv_pair_contains("Finance", "--resume") is True  # different pair: pid 201 has Finance then --resume
    # Non-existent pair
    assert _argv_pair_contains("--resume", "Marketing") is False


def test_argv_pair_ignores_substring_with_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell with a single arg containing '--resume Finance' (literal space) must NOT match."""
    _make_fake_proc(tmp_path, "300", ["bash", "-c", "echo --resume Finance"])
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    assert _argv_pair_contains("--resume", "Finance") is False


def test_session_is_alive_via_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "f6b233bc-3110-49bd-ba6e-57d49459c522"
    _make_fake_proc(tmp_path, "400", ["claude", "--resume", sid])
    monkeypatch.setattr("slackbot.process_liveness._PROC", tmp_path)
    assert session_is_alive(sid, None, None) is True


def test_session_is_alive_via_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_process_liveness.py -v
```

Expected: import errors or AttributeError on `_argv_contains` / `_argv_pair_contains`.

- [ ] **Step 3: Rewrite `src/slackbot/process_liveness.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_process_liveness.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/slackbot/process_liveness.py tests/test_process_liveness.py
uv run ruff format src/slackbot/process_liveness.py tests/test_process_liveness.py
git add src/slackbot/process_liveness.py tests/test_process_liveness.py
git commit -m "fix(liveness): argv-token matcher (split cmdline on NUL, exact-match tokens)"
```

---

## Task 3: TTL-cached liveness wrapper

**Files:**
- Create: `src/slackbot/liveness_cache.py`
- Create: `tests/test_liveness_cache.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_liveness_cache.py`:

```python
import pytest

from slackbot.liveness_cache import LivenessCache


@pytest.mark.asyncio
async def test_cache_hit_returns_memoized() -> None:
    calls: list[tuple] = []

    def probe(sid, pid, name):
        calls.append((sid, pid, name))
        return True

    clock = {"t": 1000.0}
    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: clock["t"])
    assert await cache.is_alive("s1", 100, "n") is True
    assert await cache.is_alive("s1", 100, "n") is True  # cache hit, no second call
    assert calls == [("s1", 100, "n")]


@pytest.mark.asyncio
async def test_cache_expires_after_ttl() -> None:
    calls: list[tuple] = []

    def probe(sid, pid, name):
        calls.append((sid, pid, name))
        return True

    clock = {"t": 1000.0}
    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: clock["t"])
    await cache.is_alive("s1", 100, "n")
    clock["t"] += 11.0
    await cache.is_alive("s1", 100, "n")
    assert len(calls) == 2  # ttl expired → re-probed


@pytest.mark.asyncio
async def test_cache_keyed_on_full_tuple() -> None:
    calls: list[tuple] = []

    def probe(sid, pid, name):
        calls.append((sid, pid, name))
        return True

    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: 1000.0)
    await cache.is_alive("s1", 100, "n")
    await cache.is_alive("s2", 100, "n")
    await cache.is_alive("s1", 101, "n")
    assert len(calls) == 3  # each unique tuple probed once


@pytest.mark.asyncio
async def test_probe_runs_off_event_loop() -> None:
    """Confirm probe runs via asyncio.to_thread (i.e. doesn't block)."""
    import threading

    main_thread_id = threading.get_ident()
    captured: dict = {}

    def probe(sid, pid, name):
        captured["thread"] = threading.get_ident()
        return True

    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: 1000.0)
    await cache.is_alive("s", 1, "n")
    assert captured["thread"] != main_thread_id
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_liveness_cache.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Write `src/slackbot/liveness_cache.py`**

```python
"""TTL-cached liveness probe that runs the /proc scan off the event loop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

ProbeFn = Callable[[str | None, int | None, str | None], bool]
ClockFn = Callable[[], float]


class LivenessCache:
    def __init__(
        self,
        probe: ProbeFn,
        ttl_seconds: float = 10.0,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self._probe = probe
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str | None, int | None, str | None], tuple[float, bool]] = {}

    async def is_alive(
        self, cc_session_id: str | None, cc_pid: int | None, name: str | None
    ) -> bool:
        key = (cc_session_id, cc_pid, name)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None:
            ts, value = cached
            if now - ts < self._ttl:
                return value
        result = await asyncio.to_thread(self._probe, cc_session_id, cc_pid, name)
        self._cache[key] = (now, result)
        return result
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_liveness_cache.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/liveness_cache.py tests/test_liveness_cache.py
git commit -m "feat(liveness): 10s-TTL cache, scan runs in asyncio.to_thread"
```

---

## Task 4: Transcript reader

The reader watches a single JSONL file with inotify and yields parsed message dicts as new lines arrive. Tests don't need real inotify — we'll inject a "tick" function so the reader processes whatever's on disk.

**Files:**
- Create: `src/slackbot/transcript_reader.py`
- Create: `tests/test_transcript_reader.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_transcript_reader.py`:

```python
import json
from pathlib import Path

import pytest

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
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hello back"}]}},
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
    _append(p, {"type": "user", "uuid": "u1", "parentUuid": None,
                "message": {"role": "user", "content": "ok"}})
    with p.open("a") as f:
        f.write("not json at all\n")
    _append(p, {"type": "user", "uuid": "u2", "parentUuid": None,
                "message": {"role": "user", "content": "ok2"}})
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
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "done"}]}},
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
    _append(p, {"type": "user", "uuid": "u1", "parentUuid": None,
                "message": {"role": "user", "content": "old"}})
    list(reader.drain())  # consume
    # Truncate and write new content
    p.write_text("")
    _append(p, {"type": "user", "uuid": "u2", "parentUuid": None,
                "message": {"role": "user", "content": "new"}})
    events = list(reader.drain())
    assert [e["uuid"] for e in events] == ["u2"]
    reader.close()


def test_reader_handles_string_content_legacy(tmp_path: Path) -> None:
    """Older transcripts stored content as plain string instead of array of blocks."""
    p = tmp_path / "tx.jsonl"
    p.write_text("")
    reader = TranscriptReader(p)
    reader.open()
    _append(p, {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                "message": {"role": "assistant", "content": "legacy text"}})
    events = list(reader.drain())
    assert events == [{"kind": "response", "uuid": "a1", "parentUuid": "u1",
                       "text": "legacy text"}]
    reader.close()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_transcript_reader.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Write `src/slackbot/transcript_reader.py`**

```python
"""Parse CC's JSONL transcript and yield prompt/response events for the worker.

Designed for polling (`drain()`) so tests are clock-free. Production wiring
combines `drain()` with an inotify watch — see Supervisor.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _extract_text(content: Any) -> str:
    """CC stores message.content as either a plain string or an array of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


class TranscriptReader:
    """Tail a JSONL file. Holds a byte offset and yields parsed events on drain()."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._offset = 0
        self._buffer = b""

    def open(self) -> None:
        # Start from current EOF: we want events appended after we register, not history.
        # Tests that create empty files start at 0; production sessions register on
        # SessionStart, before CC writes its first message.
        try:
            self._offset = self._path.stat().st_size
        except FileNotFoundError:
            self._offset = 0

    def close(self) -> None:
        self._buffer = b""

    def drain(self) -> Iterator[dict[str, Any]]:
        """Read any bytes appended since the last call and yield parsed events."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return
        if st.st_size < self._offset:
            # File was truncated or rotated — restart from the beginning.
            log.info("transcript %s shrank; resetting offset", self._path)
            self._offset = 0
            self._buffer = b""
        if st.st_size == self._offset:
            return
        with self._path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset += len(chunk)
        data = self._buffer + chunk
        # If we don't end on a newline, hold the trailing partial line.
        if not data.endswith(b"\n"):
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                self._buffer = data
                return
            self._buffer = data[last_nl + 1 :]
            data = data[: last_nl + 1]
        else:
            self._buffer = b""
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping malformed transcript line in %s", self._path)
                continue
            event = self._record_to_event(rec)
            if event is not None:
                yield event

    def _record_to_event(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        rtype = rec.get("type")
        uuid = rec.get("uuid")
        parent = rec.get("parentUuid")
        msg = rec.get("message")
        if rtype == "user" and isinstance(msg, dict):
            content = msg.get("content")
            text = _extract_text(content)
            if not text:
                return None
            return {"kind": "prompt", "uuid": uuid, "parentUuid": parent, "text": text}
        if rtype == "assistant" and isinstance(msg, dict):
            content = msg.get("content")
            text = _extract_text(content)
            if not text:
                return None
            return {"kind": "response", "uuid": uuid, "parentUuid": parent, "text": text}
        return None
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_transcript_reader.py -v
```

Expected: 6 passed. Note: `test_reader_emits_user_and_assistant` will need the reader to start at offset 0 for empty files — verify the `open()` logic handles that (stat on empty file = 0).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/transcript_reader.py tests/test_transcript_reader.py
git commit -m "feat(reader): JSONL transcript tail with offset cursor + partial-line buffer"
```

---

## Task 5: Registry — transcript_path column + transactional claim_name

**Files:**
- Modify: `src/slackbot/registry.py`
- Modify: `tests/test_registry.py`

- [ ] **Step 1: Append the new tests**

Append to `tests/test_registry.py`:

```python
def test_upsert_session_persists_transcript_path(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "3", transcript_path="/tmp/tx.jsonl")
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.transcript_path == "/tmp/tx.jsonl"
    reg.close()


def test_refresh_liveness_does_not_flip_status(tmp_db_path: str) -> None:
    """status is diagnostic-only now; refresh must not touch it."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "3", cc_pid=100)
    reg.set_status("s1", "ended")
    reg.refresh_liveness("s1", "main", "5", 200)
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.status == "ended"  # NO LONGER flipped to active
    assert sess.cc_pid == 200  # other fields still refreshed
    reg.close()


def test_claim_name_is_atomic(tmp_db_path: str) -> None:
    """Two concurrent claims of the same name end with exactly one row owning it."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("a", "/x", "main", "1")
    reg.upsert_session("b", "/x", "main", "2")
    # Sequential here (no asyncio); the transaction guarantee is that the SECOND
    # claim sees the FIRST's effect, so prior_thread is correctly transferred.
    reg.claim_name("a", "shared")
    reg.set_thread_ts("a", "T.1")
    prior = reg.claim_name("b", "shared")
    assert prior == "T.1"
    a = reg.get_session("a")
    b = reg.get_session("b")
    assert a is not None and a.name is None and a.slack_thread_ts is None
    assert b is not None and b.name == "shared" and b.slack_thread_ts == "T.1"
    reg.close()
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_registry.py::test_upsert_session_persists_transcript_path \
              tests/test_registry.py::test_refresh_liveness_does_not_flip_status \
              tests/test_registry.py::test_claim_name_is_atomic -v
```

Expected: failures.

- [ ] **Step 3: Update schema and migrate**

In `src/slackbot/registry.py`, modify `_SCHEMA` to add the column to the CREATE TABLE:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  cc_session_id   TEXT PRIMARY KEY,
  agent           TEXT NOT NULL DEFAULT 'claude',
  name            TEXT,
  cwd             TEXT NOT NULL,
  zellij_session  TEXT,
  zellij_pane_id  TEXT,
  slack_channel   TEXT,
  slack_thread_ts TEXT,
  cc_pid          INTEGER,
  transcript_path TEXT,
  created_at      INTEGER NOT NULL,
  last_event_at   INTEGER NOT NULL,
  status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  cc_session_id   TEXT NOT NULL,
  ts              INTEGER NOT NULL,
  kind            TEXT NOT NULL,
  payload         TEXT NOT NULL,
  slack_msg_ts    TEXT,
  FOREIGN KEY (cc_session_id) REFERENCES sessions(cc_session_id)
);

CREATE INDEX IF NOT EXISTS idx_event_log_unposted
  ON event_log(cc_session_id) WHERE slack_msg_ts IS NULL;
"""
```

In `_migrate()`, add:

```python
        if "transcript_path" not in columns:
            self._c().execute("ALTER TABLE sessions ADD COLUMN transcript_path TEXT")
```

In the `Session` dataclass, add field:

```python
    transcript_path: str | None
```

In `_row_to_session`, add:

```python
        transcript_path=row["transcript_path"],
```

- [ ] **Step 4: Extend upsert_session**

Replace `upsert_session` signature and body:

```python
    def upsert_session(
        self,
        cc_session_id: str,
        cwd: str,
        zellij_session: str | None,
        zellij_pane_id: str | None,
        agent: str = "claude",
        slack_channel: str | None = None,
        cc_pid: int | None = None,
        transcript_path: str | None = None,
    ) -> None:
        now = int(time.time())
        self._c().execute(
            """
            INSERT INTO sessions (cc_session_id, agent, cwd, zellij_session, zellij_pane_id,
                                  slack_channel, cc_pid, transcript_path,
                                  created_at, last_event_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(cc_session_id) DO UPDATE SET
              agent = excluded.agent,
              cwd = excluded.cwd,
              zellij_session = excluded.zellij_session,
              zellij_pane_id = excluded.zellij_pane_id,
              slack_channel = excluded.slack_channel,
              cc_pid = COALESCE(excluded.cc_pid, sessions.cc_pid),
              transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
              last_event_at = excluded.last_event_at,
              status = 'active'
            """,
            (
                cc_session_id,
                agent,
                cwd,
                zellij_session,
                zellij_pane_id,
                slack_channel,
                cc_pid,
                transcript_path,
                now,
                now,
            ),
        )
```

- [ ] **Step 5: Drop status flip from refresh_liveness**

Replace `refresh_liveness`:

```python
    def refresh_liveness(
        self,
        cc_session_id: str,
        zellij_session: str | None,
        zellij_pane_id: str | None,
        cc_pid: int | None,
    ) -> None:
        """Update mutable runtime fields. Does NOT touch status — that is now
        diagnostic-only; the reply path uses session_is_alive directly."""
        sets: list[str] = ["last_event_at = ?"]
        params: list[object] = [int(time.time())]
        if zellij_session:
            sets.append("zellij_session = ?")
            params.append(zellij_session)
        if zellij_pane_id:
            sets.append("zellij_pane_id = ?")
            params.append(zellij_pane_id)
        if cc_pid is not None and cc_pid > 0:
            sets.append("cc_pid = ?")
            params.append(cc_pid)
        params.append(cc_session_id)
        self._c().execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE cc_session_id = ?",
            params,
        )
```

- [ ] **Step 6: Make claim_name transactional**

Replace `claim_name`:

```python
    def claim_name(self, cc_session_id: str, name: str) -> str | None:
        """Claim `name` for `cc_session_id`. Returns prior holder's thread_ts (or None).

        Wrapped in BEGIN IMMEDIATE/COMMIT so a concurrent claim cannot leave
        two rows owning the same name.
        """
        conn = self._c()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT slack_channel FROM sessions WHERE cc_session_id = ?",
                (cc_session_id,),
            ).fetchone()
            current_channel = current["slack_channel"] if current else None
            prior = conn.execute(
                "SELECT cc_session_id, slack_channel, slack_thread_ts FROM sessions "
                "WHERE name = ? AND cc_session_id != ?",
                (name, cc_session_id),
            ).fetchone()
            prior_thread: str | None = None
            if prior and prior["slack_channel"] == current_channel:
                prior_thread = prior["slack_thread_ts"]
            if prior:
                conn.execute(
                    "UPDATE sessions SET name = NULL, slack_thread_ts = NULL "
                    "WHERE cc_session_id = ?",
                    (prior["cc_session_id"],),
                )
            conn.execute(
                "UPDATE sessions SET name = ?, last_event_at = ? WHERE cc_session_id = ?",
                (name, int(time.time()), cc_session_id),
            )
            if prior_thread:
                conn.execute(
                    "UPDATE sessions SET slack_thread_ts = ? WHERE cc_session_id = ?",
                    (prior_thread, cc_session_id),
                )
            conn.execute("COMMIT")
            return prior_thread
        except Exception:
            conn.execute("ROLLBACK")
            raise
```

- [ ] **Step 7: Drop time clause from find_recoverable_session**

Replace `find_recoverable_session`:

```python
    def find_recoverable_session(
        self,
        zellij_session: str | None,
        cwd: str,
        agent: str,
        exclude_sid: str,
    ) -> Session | None:
        """Find any named predecessor in the same workspace. The caller (handler)
        decides whether the candidate is actually dead via session_is_alive."""
        row = (
            self._c()
            .execute(
                """
                SELECT * FROM sessions
                WHERE name IS NOT NULL
                  AND cwd = ?
                  AND zellij_session IS ?
                  AND agent = ?
                  AND cc_session_id != ?
                ORDER BY last_event_at DESC
                LIMIT 1
                """,
                (cwd, zellij_session, agent, exclude_sid),
            )
            .fetchone()
        )
        return _row_to_session(row) if row else None
```

- [ ] **Step 8: Run all registry tests**

```bash
uv run pytest tests/test_registry.py -v
```

Expected: all pass (existing + 3 new).

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/registry.py tests/test_registry.py
git commit -m "feat(registry): transcript_path column, transactional claim_name, status-flip dropped"
```

---

## Task 6: Worker

The worker is the central new component. It owns a queue and processes events serially. It calls the actuator and Slack, but never decides whether a session is alive — that's the supervisor's job.

**Files:**
- Create: `src/slackbot/worker.py`
- Create: `tests/test_worker.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_worker.py`:

```python
import asyncio
from dataclasses import dataclass, field

import pytest

from slackbot.worker import Worker


@dataclass
class FakeSlackIO:
    posts: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    top_level_posts: list[str] = field(default_factory=list)
    _ts: int = 0

    def channel_for_agent(self, agent: str) -> str:
        return f"C-{agent.upper()}"

    async def post_in_thread(self, thread_ts, text, channel=None):
        self.posts.append((thread_ts, text))
        self._ts += 1
        return f"thr.{self._ts}"

    async def post_top_level(self, text, channel=None):
        self.top_level_posts.append(text)
        self._ts += 1
        return f"top.{self._ts}"

    async def edit_top_level(self, ts, text, channel=None):
        pass

    async def react(self, ts, emoji, channel=None):
        self.reacts.append((ts, emoji))


@dataclass
class FakeActuator:
    deliveries: list[tuple[str, str, str]] = field(default_factory=list)

    async def deliver(self, session, pane_id, text):
        self.deliveries.append((session, pane_id, text))


def _bound_session(reg, sid, agent="claude"):
    """Helper: create a registered+named session with a thread."""
    reg.upsert_session(sid, "/x", "main", "13", agent=agent, slack_channel="C-CLAUDE")
    reg.set_name(sid, "myproj")
    reg.set_thread_ts(sid, "TOP.1")


@pytest.mark.asyncio
async def test_worker_mirrors_assistant_response(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1",
                          "text": "hello"})
    await worker.stop()

    assert slack.posts == [("TOP.1", "[Claude] 🤖 hello")]
    reg.close()


@pytest.mark.asyncio
async def test_worker_dedupes_by_uuid(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    ev = {"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hello"}
    await worker.enqueue(ev)
    await worker.enqueue(ev)
    await worker.stop()

    assert len(slack.posts) == 1
    reg.close()


@pytest.mark.asyncio
async def test_worker_delivers_slack_reply_to_pane(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    actuator = FakeActuator()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=actuator)
    await worker.start()

    await worker.enqueue({"kind": "slack_reply", "text": "do X", "msg_ts": "MSG.1"})
    await worker.stop()

    assert actuator.deliveries == [("main", "13", "do X")]
    assert ("MSG.1", "white_check_mark") in slack.reacts
    reg.close()


@pytest.mark.asyncio
async def test_worker_suppresses_echoed_prompt_after_delivery(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    # Slack reply is delivered to pane → echo expected.
    await worker.enqueue({"kind": "slack_reply", "text": "ping", "msg_ts": "MSG.1"})
    # Transcript reader will see the user typing it and emit prompt with same text.
    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "ping"})
    await worker.stop()

    # The slack reply's reaction was added, but the prompt was suppressed (not mirrored).
    assert ("MSG.1", "white_check_mark") in slack.reacts
    assert slack.posts == []  # no 👤 ping mirrored
    reg.close()


@pytest.mark.asyncio
async def test_worker_mirrors_organic_prompt(tmp_db_path: str) -> None:
    """A prompt that didn't come from a Slack delivery IS mirrored."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None,
                          "text": "what's up"})
    await worker.stop()

    assert slack.posts == [("TOP.1", "[Claude] 👤 what's up")]
    reg.close()


@pytest.mark.asyncio
async def test_worker_skips_unbound_session(tmp_db_path: str) -> None:
    """If the session has no thread (not /rn'd or auto-recovered), buffer or skip."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13", agent="claude")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1",
                          "text": "hi"})
    await worker.stop()

    # No thread yet → no Slack post. Event log holds it for replay.
    assert slack.posts == []
    reg.close()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_worker.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Write `src/slackbot/worker.py`**

```python
"""Per-conversation worker. Owns the queue, mirrors transcript events to
Slack, delivers Slack replies into the pane, suppresses delivery echo."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from slackbot.events import format_event, top_level_text
from slackbot.registry import Registry

log = logging.getLogger(__name__)

# Cap how many uuids we remember per worker so memory stays bounded.
_MAX_REMEMBERED_UUIDS = 1000
# Cap on pending echo entries.
_MAX_PENDING_ECHO = 64


class _SlackIOProto(Protocol):
    def channel_for_agent(self, agent: str) -> str: ...
    async def post_top_level(self, text: str, channel: str | None = None) -> str: ...
    async def post_in_thread(
        self, thread_ts: str, text: str, channel: str | None = None
    ) -> str: ...
    async def edit_top_level(self, ts: str, text: str, channel: str | None = None) -> None: ...
    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None: ...


class _ActuatorProto(Protocol):
    async def deliver(self, session: str, pane_id: str, text: str) -> None: ...


class Worker:
    def __init__(
        self,
        sid: str,
        reg: Registry,
        slack: _SlackIOProto,
        actuator: _ActuatorProto,
    ) -> None:
        self._sid = sid
        self._reg = reg
        self._slack = slack
        self._actuator = actuator
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._posted_uuids: list[str] = []  # FIFO bounded
        self._pending_echo: list[str] = []  # FIFO bounded
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def enqueue(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)

    async def stop(self) -> None:
        """Drain the queue, then cancel the task."""
        await self._queue.join()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._dispatch(event)
            except Exception:
                log.exception("worker[%s] dispatch failed for %r", self._sid, event)
            finally:
                self._queue.task_done()

    async def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        method = getattr(self, f"_on_{kind}", None)
        if method is None:
            log.debug("worker[%s] no handler for kind=%s", self._sid, kind)
            return
        await method(event)

    async def _on_prompt(self, ev: dict[str, Any]) -> None:
        text = ev.get("text", "")
        if text in self._pending_echo:
            self._pending_echo.remove(text)
            log.debug("worker[%s] echo suppressed: %r", self._sid, text)
            return
        await self._mirror("prompt", {"text": text}, ev.get("uuid"))

    async def _on_response(self, ev: dict[str, Any]) -> None:
        uuid = ev.get("uuid")
        if uuid in self._posted_uuids:
            return
        await self._mirror("response", {"text": ev.get("text", "")}, uuid)

    async def _on_notification(self, ev: dict[str, Any]) -> None:
        data = {
            "message": ev.get("message", ""),
            "tool_request": ev.get("tool_request", ""),
            "context": ev.get("context", ""),
        }
        await self._mirror("notification", data, uuid=None)

    async def _on_error(self, ev: dict[str, Any]) -> None:
        await self._mirror("error", {"text": ev.get("text", "")}, uuid=None)

    async def _on_slack_reply(self, ev: dict[str, Any]) -> None:
        text = ev["text"]
        msg_ts = ev.get("msg_ts", "")
        sess = self._reg.get_session(self._sid)
        if sess is None:
            return
        channel = sess.slack_channel
        if not sess.zellij_session or not sess.zellij_pane_id:
            await self._slack.post_in_thread(
                sess.slack_thread_ts or "",
                "❌ delivery failed: session has no pane info",
                channel=channel,
            )
            return
        try:
            await self._actuator.deliver(sess.zellij_session, sess.zellij_pane_id, text)
        except Exception as exc:  # noqa: BLE001 — surface as warning
            await self._slack.post_in_thread(
                sess.slack_thread_ts or "",
                f"❌ delivery failed: {exc}",
                channel=channel,
            )
            await self._slack.react(msg_ts, "warning", channel=channel)
            return
        self._remember_echo(text)
        await self._slack.react(msg_ts, "white_check_mark", channel=channel)

    async def _mirror(
        self, kind: str, data: dict[str, Any], uuid: str | None
    ) -> None:
        sess = self._reg.get_session(self._sid)
        if sess is None:
            return
        if sess.name is None or sess.slack_thread_ts is None:
            # Buffer for replay when /rn or auto-recovery binds the thread.
            self._reg.buffer_event(self._sid, kind, json.dumps({**data, "agent": sess.agent}))
            return
        text = format_event(kind, {**data, "agent": sess.agent})
        ts = await self._slack.post_in_thread(
            sess.slack_thread_ts, text, channel=sess.slack_channel
        )
        if uuid:
            self._remember_uuid(uuid)
        # Record posted for traceability.
        evt_id = self._reg.buffer_event(
            self._sid, kind, json.dumps({**data, "agent": sess.agent})
        )
        self._reg.mark_event_posted(evt_id, ts)

    def _remember_uuid(self, uuid: str) -> None:
        self._posted_uuids.append(uuid)
        if len(self._posted_uuids) > _MAX_REMEMBERED_UUIDS:
            self._posted_uuids = self._posted_uuids[-_MAX_REMEMBERED_UUIDS:]

    def _remember_echo(self, text: str) -> None:
        self._pending_echo.append(text)
        if len(self._pending_echo) > _MAX_PENDING_ECHO:
            self._pending_echo = self._pending_echo[-_MAX_PENDING_ECHO:]
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_worker.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/worker.py tests/test_worker.py
git commit -m "feat(worker): per-conversation async loop with uuid dedupe + echo suppression"
```

---

## Task 7: Supervisor

The supervisor spawns workers on demand and runs the transcript-reader poll loop. It also implements the reaper.

**Files:**
- Create: `src/slackbot/supervisor.py`
- Create: `tests/test_supervisor.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_supervisor.py`:

```python
import asyncio

import pytest

from slackbot.registry import Registry
from slackbot.supervisor import Supervisor


class _FakeSlack:
    def channel_for_agent(self, agent):
        return f"C-{agent.upper()}"

    async def post_top_level(self, text, channel=None):
        return "top.1"

    async def post_in_thread(self, thread_ts, text, channel=None):
        return "thr.1"

    async def edit_top_level(self, ts, text, channel=None):
        pass

    async def react(self, ts, emoji, channel=None):
        pass


class _FakeActuator:
    async def deliver(self, session, pane_id, text):
        pass


@pytest.mark.asyncio
async def test_get_or_create_spawns_one_worker_per_sid(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    sup = Supervisor(reg=reg, slack=_FakeSlack(), actuator=_FakeActuator())
    w1 = await sup.get_or_create("s1")
    w2 = await sup.get_or_create("s1")
    assert w1 is w2
    await sup.shutdown()
    reg.close()


@pytest.mark.asyncio
async def test_reap_removes_idle_workers(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    clock = {"t": 1000.0}
    sup = Supervisor(
        reg=reg, slack=_FakeSlack(), actuator=_FakeActuator(),
        idle_seconds=5, clock=lambda: clock["t"],
    )
    w = await sup.get_or_create("s1")
    # advance the clock past idle window
    clock["t"] += 10.0
    await sup.reap_once()
    assert "s1" not in sup._workers
    assert w._task is None or w._task.done()
    await sup.shutdown()
    reg.close()


@pytest.mark.asyncio
async def test_recent_activity_keeps_worker_alive(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    clock = {"t": 1000.0}
    sup = Supervisor(
        reg=reg, slack=_FakeSlack(), actuator=_FakeActuator(),
        idle_seconds=5, clock=lambda: clock["t"],
    )
    await sup.get_or_create("s1")
    clock["t"] += 2.0
    await sup.touch("s1")
    clock["t"] += 4.0  # 4s after touch — still inside window
    await sup.reap_once()
    assert "s1" in sup._workers
    await sup.shutdown()
    reg.close()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_supervisor.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Write `src/slackbot/supervisor.py`**

```python
"""Worker lifecycle: spawn on first event, reap on idle."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from slackbot.registry import Registry
from slackbot.transcript_reader import TranscriptReader
from slackbot.worker import Worker

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(
        self,
        reg: Registry,
        slack,
        actuator,
        idle_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reg = reg
        self._slack = slack
        self._actuator = actuator
        self._idle = idle_seconds
        self._clock = clock
        self._workers: dict[str, Worker] = {}
        self._last_touch: dict[str, float] = {}
        self._readers: dict[str, TranscriptReader] = {}

    async def get_or_create(self, sid: str) -> Worker:
        worker = self._workers.get(sid)
        if worker is None:
            worker = Worker(sid=sid, reg=self._reg, slack=self._slack, actuator=self._actuator)
            await worker.start()
            self._workers[sid] = worker
        self._last_touch[sid] = self._clock()
        return worker

    async def touch(self, sid: str) -> None:
        if sid in self._workers:
            self._last_touch[sid] = self._clock()

    def attach_reader(self, sid: str, transcript_path: str) -> None:
        if sid in self._readers:
            return
        reader = TranscriptReader(Path(transcript_path))
        reader.open()
        self._readers[sid] = reader

    def detach_reader(self, sid: str) -> None:
        reader = self._readers.pop(sid, None)
        if reader is not None:
            reader.close()

    async def pump_readers(self) -> None:
        """Drain every reader once and enqueue events into the right worker.

        Called periodically (or driven by inotify in production). Keeping it
        polling-shaped makes tests deterministic.
        """
        for sid, reader in list(self._readers.items()):
            worker = await self.get_or_create(sid)
            for event in reader.drain():
                await worker.enqueue(event)

    async def reap_once(self) -> None:
        now = self._clock()
        for sid in list(self._workers.keys()):
            last = self._last_touch.get(sid, now)
            if now - last >= self._idle:
                worker = self._workers.pop(sid)
                self._last_touch.pop(sid, None)
                self.detach_reader(sid)
                await worker.stop()
                log.info("reaped idle worker %s", sid)

    async def shutdown(self) -> None:
        for sid in list(self._workers.keys()):
            worker = self._workers.pop(sid)
            self.detach_reader(sid)
            await worker.stop()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_supervisor.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): worker spawn/reap lifecycle + transcript reader attach"
```

---

## Task 8: Refactor handlers — dispatch into supervisor

`handlers.py` becomes the bridge from HTTP events to supervisor. It still owns auto-recovery (only at SessionStart) and event-log buffering for pre-name events.

**Files:**
- Modify: `src/slackbot/handlers.py`
- Modify: `tests/test_handlers.py`

- [ ] **Step 1: Replace `src/slackbot/handlers.py`**

```python
"""HTTP event handlers: turn POSTed hook events into supervisor calls."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from slackbot.events import top_level_text
from slackbot.process_liveness import session_is_alive
from slackbot.registry import Registry
from slackbot.supervisor import Supervisor

log = logging.getLogger(__name__)


class EventHandlers:
    def __init__(self, reg: Registry, supervisor: Supervisor, slack) -> None:
        self._reg = reg
        self._sup = supervisor
        self._slack = slack

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        sid = event.get("session_id", "")
        if not sid:
            log.warning("event missing session_id: %r", event)
            return
        method = getattr(self, f"_on_{kind}", None)
        if method is None:
            log.warning("no handler for kind=%s", kind)
            return
        await method(event)
        await self._sup.touch(sid)

    async def _on_start(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        prior = self._reg.get_session(sid)
        agent = _agent(ev.get("agent"))
        channel = self._slack.channel_for_agent(agent)
        cwd = ev["cwd"]
        zellij_session = ev.get("zellij_session")
        cc_pid = _int_or_none(ev.get("cc_pid"))
        transcript_path = ev.get("transcript_path") or None

        self._reg.upsert_session(
            sid,
            cwd,
            zellij_session,
            ev.get("zellij_pane_id"),
            agent=agent,
            slack_channel=channel,
            cc_pid=cc_pid,
            transcript_path=transcript_path,
        )

        if transcript_path:
            self._sup.attach_reader(sid, transcript_path)
        await self._sup.get_or_create(sid)

        if prior and prior.name and prior.slack_thread_ts:
            await self._slack.edit_top_level(
                prior.slack_thread_ts,
                top_level_text(prior.name, prior.cwd, "active", prior.agent),
                channel=prior.slack_channel,
            )
            return

        recovered = self._reg.find_recoverable_session(
            zellij_session=zellij_session, cwd=cwd, agent=agent, exclude_sid=sid
        )
        if recovered and recovered.name:
            # Only inherit when the recoverable predecessor is not alive.
            if not session_is_alive(
                recovered.cc_session_id, recovered.cc_pid, recovered.name
            ):
                await self._on_name(
                    {
                        "kind": "name",
                        "session_id": sid,
                        "name": recovered.name,
                        "auto_recovered": True,
                    }
                )
                return

        if _auto_registers(agent):
            await self._on_name(
                {
                    "kind": "name",
                    "session_id": sid,
                    "name": _auto_name(agent, cwd, sid),
                }
            )

    async def _on_name(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        new_name = ev["name"]
        sess = self._reg.get_session(sid)
        if sess is None:
            log.warning("name event for unknown session %s", sid)
            return
        if sess.name == new_name:
            return
        prior_thread = self._reg.claim_name(sid, new_name)
        sess = self._reg.get_session(sid)
        assert sess is not None
        if prior_thread:
            marker = "auto-rebound" if ev.get("auto_recovered") else "resumed"
            await self._slack.post_in_thread(
                prior_thread,
                f"─── 🔄 {marker} in new session @ {_iso_now()} ───",
                channel=sess.slack_channel,
            )
        else:
            ts = await self._slack.post_top_level(
                top_level_text(new_name, sess.cwd, "active", sess.agent),
                channel=sess.slack_channel,
            )
            self._reg.set_thread_ts(sid, ts)

        # Replay buffered events into the worker.
        worker = await self._sup.get_or_create(sid)
        for buffered in self._reg.drain_unposted(sid):
            import json
            data = json.loads(buffered.payload)
            replay = {"kind": buffered.kind, **data}
            await worker.enqueue(replay)
            self._reg.mark_event_posted(buffered.id, "replayed")

    async def _on_end(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        self._reg.set_status(sid, "ended")
        sess = self._reg.get_session(sid)
        if sess and sess.name and sess.slack_thread_ts:
            await self._slack.edit_top_level(
                sess.slack_thread_ts,
                top_level_text(sess.name, sess.cwd, "ended", sess.agent),
                channel=sess.slack_channel,
            )
        self._sup.detach_reader(sid)

    async def _on_notification(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        # Refresh runtime fields opportunistically (cc_pid keeps drift in check).
        self._refresh_runtime(sid, ev)
        worker = await self._sup.get_or_create(sid)
        await worker.enqueue(
            {
                "kind": "notification",
                "message": ev.get("message", ""),
                "tool_request": ev.get("tool_request", ""),
                "context": ev.get("context", ""),
            }
        )

    def _refresh_runtime(self, sid: str, ev: dict[str, Any]) -> None:
        sess = self._reg.get_session(sid)
        if sess is None:
            return
        cc_pid = _int_or_none(ev.get("cc_pid"))
        self._reg.refresh_liveness(
            sid,
            ev.get("zellij_session") or sess.zellij_session,
            ev.get("zellij_pane_id") or sess.zellij_pane_id,
            cc_pid,
        )


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _agent(value: object) -> str:
    agent = str(value or "claude").lower()
    if agent in {"claude", "codex", "gemini"}:
        return agent
    return "claude"


def _auto_registers(agent: str) -> bool:
    return agent in {"codex", "gemini"}


def _auto_name(agent: str, cwd: str, sid: str) -> str:
    project = PurePath(cwd).name or "session"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", project).strip("-").lower() or "session"
    short_sid = re.sub(r"[^a-zA-Z0-9]+", "", sid)[:8] or "session"
    return f"{agent}-{slug}-{short_sid}"


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 2: Update `tests/test_handlers.py` to use the new constructor**

Look at each test in `tests/test_handlers.py`. Replace `EventHandlers(reg, slack)` with `EventHandlers(reg, supervisor, slack)` where `supervisor` is a fresh `Supervisor` instance constructed similarly to other tests (use the FakeSlackIO + a no-op actuator). For each test fixture, also add:

```python
@pytest.fixture
def sup(reg: Registry):
    from slackbot.supervisor import Supervisor
    class _NoopActuator:
        async def deliver(self, *a, **kw): pass
    return Supervisor(reg=reg, slack=FakeSlackIO(), actuator=_NoopActuator())
```

And in each test that uses `EventHandlers(reg, slack)`, change to `EventHandlers(reg, sup, slack)`. (Note: `slack` argument and `sup`'s `slack` may differ; pass the same instance for inspection.)

The minimal viable change: rewrite each test's instantiation. Run after each batch.

- [ ] **Step 3: Run all handler tests**

```bash
uv run pytest tests/test_handlers.py -v
```

Expected: existing assertions continue to pass with the supervisor present. Adjust any test that depended on `EventHandlers` posting directly — the new model routes mirroring through the worker only on transcript events, not on `_on_prompt`/`_on_response`.

(Note for the implementer: the old `_on_prompt` and `_on_response` event paths in the handler are GONE. Tests that exercised those should be reframed as worker tests, or simply deleted. The handler now only handles `start`, `name`, `end`, `notification`.)

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/handlers.py tests/test_handlers.py
git commit -m "refactor(handlers): dispatch into supervisor; transcript drives prompt/response"
```

---

## Task 9: Refactor reply_router — enqueue, don't act

**Files:**
- Modify: `src/slackbot/reply_router.py`
- Modify: `tests/test_reply_router.py`

- [ ] **Step 1: Replace `src/slackbot/reply_router.py`**

```python
"""Slack thread replies are enqueued into the matching worker; the worker
owns delivery, echo suppression, and reaction."""

from __future__ import annotations

import logging

from slackbot.liveness_cache import LivenessCache
from slackbot.registry import Registry
from slackbot.supervisor import Supervisor

log = logging.getLogger(__name__)


class ReplyRouter:
    def __init__(
        self,
        reg: Registry,
        supervisor: Supervisor,
        liveness: LivenessCache,
        slack,
    ) -> None:
        self._reg = reg
        self._sup = supervisor
        self._liveness = liveness
        self._slack = slack

    async def on_reply(
        self, channel: str, thread_ts: str, text: str, msg_ts: str
    ) -> None:
        sess = self._reg.get_session_by_thread(thread_ts, channel)
        if sess is None:
            log.debug("reply for unknown thread %s ignored", thread_ts)
            return
        if not await self._liveness.is_alive(sess.cc_session_id, sess.cc_pid, sess.name):
            self._reg.set_status(sess.cc_session_id, "ended")
            await self._slack.post_in_thread(
                thread_ts,
                "⚠️ No running CC process found for this session. "
                "Reply not sent — start a new CC session in this workspace and "
                "it will auto-rebind to this thread.",
                channel=channel,
            )
            await self._slack.react(msg_ts, "no_entry_sign", channel=channel)
            return
        worker = await self._sup.get_or_create(sess.cc_session_id)
        await worker.enqueue({"kind": "slack_reply", "text": text, "msg_ts": msg_ts})
```

- [ ] **Step 2: Update `tests/test_reply_router.py`**

Replace the existing tests with a single test that confirms enqueueing happens, then 1 test confirming dead-session rejection:

```python
from dataclasses import dataclass, field

import pytest

from slackbot.liveness_cache import LivenessCache
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.supervisor import Supervisor


@dataclass
class FakeSlack:
    thread_posts: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)

    def channel_for_agent(self, agent):
        return f"C-{agent.upper()}"

    async def post_top_level(self, text, channel=None):
        return "top.1"

    async def post_in_thread(self, thread_ts, text, channel=None):
        self.thread_posts.append((thread_ts, text))
        return "thr.1"

    async def edit_top_level(self, ts, text, channel=None):
        pass

    async def react(self, ts, emoji, channel=None):
        self.reacts.append((ts, emoji))


class _NoopActuator:
    async def deliver(self, *a, **kw):
        pass


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


def _alive_cache(alive: bool) -> LivenessCache:
    return LivenessCache(lambda *_: alive, ttl_seconds=10, clock=lambda: 0.0)


@pytest.mark.asyncio
async def test_reply_enqueues_into_worker(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3", agent="claude", slack_channel="C-CLAUDE")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    router = ReplyRouter(reg=reg, supervisor=sup, liveness=_alive_cache(True), slack=slack)
    await router.on_reply(
        channel="C-CLAUDE", thread_ts="TOP.1", text="do X", msg_ts="MSG.1"
    )
    # Worker exists for s1 with a pending event.
    worker = sup._workers["s1"]
    assert worker._queue.qsize() == 1
    await sup.shutdown()


@pytest.mark.asyncio
async def test_reply_to_dead_session_rejects(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3", agent="claude", slack_channel="C-CLAUDE")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    router = ReplyRouter(reg=reg, supervisor=sup, liveness=_alive_cache(False), slack=slack)
    await router.on_reply(channel="C-CLAUDE", thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert any("No running CC process" in t for _, t in slack.thread_posts)
    assert ("MSG.1", "no_entry_sign") in slack.reacts
    sess = reg.get_session("s1")
    assert sess is not None and sess.status == "ended"
    await sup.shutdown()
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_reply_router.py -v
```

Expected: 2 passed.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/reply_router.py tests/test_reply_router.py
git commit -m "refactor(reply_router): enqueue Slack reply into worker"
```

---

## Task 10: systemd notify helper

**Files:**
- Create: `src/slackbot/sd_notify.py`
- Create: `tests/test_sd_notify.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_sd_notify.py`:

```python
import os
import socket
from pathlib import Path

import pytest

from slackbot.sd_notify import notify, ready, watchdog


def test_noop_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # Must not raise even though there's nowhere to send.
    notify("WATCHDOG=1")
    ready()
    watchdog()


def test_sends_message_to_unix_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "notify.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    sock.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
    notify("WATCHDOG=1")
    data, _ = sock.recvfrom(1024)
    assert data == b"WATCHDOG=1"
    sock.close()
    os.unlink(sock_path)
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_sd_notify.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Write `src/slackbot/sd_notify.py`**

```python
"""Thin sd_notify wrapper. No external deps — Unix datagram socket only."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(message: str) -> None:
    """Send a single sd_notify message. Silent no-op when NOTIFY_SOCKET unset."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    # systemd uses an abstract socket if the path starts with '@'.
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode("utf-8"), sock_path)
    except OSError as exc:
        log.warning("sd_notify failed: %s", exc)


def ready() -> None:
    notify("READY=1")


def watchdog() -> None:
    notify("WATCHDOG=1")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_sd_notify.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/sd_notify.py tests/test_sd_notify.py
git commit -m "feat(sd_notify): READY/WATCHDOG helpers for systemd Type=notify"
```

---

## Task 11: Wire it up in `__main__`

**Files:**
- Modify: `src/slackbot/__main__.py`
- Delete: `src/slackbot/dedupe.py`

- [ ] **Step 1: Replace `src/slackbot/__main__.py`**

```python
"""Daemon entry point. Wires supervisor, transcript readers, watchdogs."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from slackbot import sd_notify
from slackbot.config import load_config
from slackbot.handlers import EventHandlers
from slackbot.liveness_cache import LivenessCache
from slackbot.logging_setup import configure as configure_logging
from slackbot.process_liveness import session_is_alive
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.server import make_app
from slackbot.slack_io import SlackIO
from slackbot.supervisor import Supervisor
from slackbot.zellij_io import ZellijActuator

log = logging.getLogger("slackbot.main")

_READER_POLL_INTERVAL = 0.5
_REAPER_INTERVAL = 30.0
_INACTIVITY_RECONNECT = 90.0
_WATCHDOG_INTERVAL = 60.0


async def reader_pump(supervisor: Supervisor) -> None:
    while True:
        try:
            await supervisor.pump_readers()
        except Exception:
            log.exception("reader pump iteration failed")
        await asyncio.sleep(_READER_POLL_INTERVAL)


async def reaper(supervisor: Supervisor) -> None:
    while True:
        try:
            await supervisor.reap_once()
        except Exception:
            log.exception("reaper iteration failed")
        await asyncio.sleep(_REAPER_INTERVAL)


async def watchdog_heartbeat(get_last_event: callable) -> None:
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL)
        sd_notify.watchdog()


async def socket_health_watchdog(socket_handler, get_last_event: callable) -> None:
    """Force a Socket Mode reconnect if no event has arrived in INACTIVITY threshold."""
    while True:
        await asyncio.sleep(30.0)
        client = socket_handler.client
        if client is None:
            continue
        last = get_last_event()
        now = asyncio.get_event_loop().time()
        if now - last > _INACTIVITY_RECONNECT:
            log.warning("no Slack events in %ss — forcing reconnect", _INACTIVITY_RECONNECT)
            try:
                await client.disconnect()
                await asyncio.sleep(1)
                await client.connect()
            except Exception:
                log.exception("watchdog reconnect failed")


async def amain() -> None:
    cfg = load_config()
    configure_logging(cfg.log_level)
    log.info("starting claude-slack-bot port=%d channel=%s", cfg.port, cfg.slack_channel_id)

    reg = Registry(cfg.db_path)
    reg.open()

    web_client = AsyncWebClient(token=cfg.slack_bot_token)
    slack_io = SlackIO(web_client, cfg.slack_channel_id, cfg.agent_channels)
    actuator = ZellijActuator()
    supervisor = Supervisor(reg=reg, slack=slack_io, actuator=actuator)
    liveness = LivenessCache(session_is_alive)
    handlers = EventHandlers(reg, supervisor, slack_io)
    router = ReplyRouter(reg=reg, supervisor=supervisor, liveness=liveness, slack=slack_io)

    bolt = AsyncApp(token=cfg.slack_bot_token, client=web_client)
    loop = asyncio.get_running_loop()
    last_event_at = [loop.time()]

    @bolt.event("message")
    async def on_message(event, logger):
        last_event_at[0] = loop.time()
        sd_notify.watchdog()
        if event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        text = event.get("text", "")
        msg_ts = event.get("ts", "")
        channel = event.get("channel", "")
        log.info(
            "thread reply received: channel=%s thread_ts=%s msg_ts=%s len=%d",
            channel,
            thread_ts,
            msg_ts,
            len(text),
        )
        await router.on_reply(channel=channel, thread_ts=thread_ts, text=text, msg_ts=msg_ts)

    socket_handler = AsyncSocketModeHandler(bolt, cfg.slack_app_token)
    http_app = make_app(handlers)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    http_site = web.TCPSite(http_runner, "127.0.0.1", cfg.port)
    await http_site.start()
    log.info("http event endpoint listening on 127.0.0.1:%d", cfg.port)

    sd_notify.ready()

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(socket_handler.start_async()))
    tasks.append(asyncio.create_task(reader_pump(supervisor)))
    tasks.append(asyncio.create_task(reaper(supervisor)))
    tasks.append(asyncio.create_task(watchdog_heartbeat(lambda: last_event_at[0])))
    tasks.append(
        asyncio.create_task(socket_health_watchdog(socket_handler, lambda: last_event_at[0]))
    )

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await supervisor.shutdown()
        await http_runner.cleanup()
        reg.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Delete the old dedupe module and its test**

```bash
git rm src/slackbot/dedupe.py tests/test_dedupe.py
```

- [ ] **Step 3: Verify imports**

```bash
uv run python -c "from slackbot.__main__ import main; print('ok')"
```

Expected: `ok`. If any test still imports `slackbot.dedupe`, fix the import.

- [ ] **Step 4: Full test run**

```bash
uv run pytest -v
```

Expected: all tests pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
git add src/slackbot/__main__.py src/slackbot/dedupe.py tests/test_dedupe.py
git commit -m "feat(main): wire supervisor + reader pump + reaper + sd_notify"
```

---

## Task 12: Update session_start hook to send transcript_path

**Files:**
- Modify: `hooks/session_start.sh`

- [ ] **Step 1: Edit `hooks/session_start.sh`**

Replace the payload-building block to include `transcript_path`:

```bash
transcript_path="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
cc_pid="$PPID"
payload="$(jq -n \
  --arg sid "$sid" \
  --arg agent "$agent" \
  --arg cwd "${cwd:-$PWD}" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" \
  --arg zp "${ZELLIJ_PANE_ID:-}" \
  --arg tx "$transcript_path" \
  --argjson resumed "$resumed_flag" \
  --argjson cc_pid "$cc_pid" \
  '{v:1,kind:"start",session_id:$sid,agent:$agent,cwd:$cwd,
    zellij_session:$zs,zellij_pane_id:$zp,transcript_path:$tx,
    resumed:$resumed,cc_pid:$cc_pid}')"
```

(Replace the existing `payload="$(jq -n ...` block in the file.)

- [ ] **Step 2: Lint + reinstall**

```bash
shellcheck hooks/session_start.sh
bash hooks/install.sh
```

Expected: shellcheck clean.

- [ ] **Step 3: Commit**

```bash
git add hooks/session_start.sh
git commit -m "feat(hooks): session_start sends transcript_path so daemon can tail"
```

---

## Task 13: Systemd unit — Type=notify + WatchdogSec

**Files:**
- Modify: `systemd/claude-slack-bot.service`

- [ ] **Step 1: Edit `systemd/claude-slack-bot.service`**

Add `Type=notify` and `WatchdogSec=600` to the `[Service]` section:

```ini
[Service]
Type=notify
EnvironmentFile=%h/.config/claude-slack-bot/env
WorkingDirectory=%h/git/priv/claude-slack-bot
ExecStart=%h/git/priv/claude-slack-bot/.venv/bin/python -m slackbot
Restart=always
RestartSec=3
WatchdogSec=600
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 2: Reload and restart**

```bash
cp systemd/claude-slack-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart claude-slack-bot
systemctl --user status claude-slack-bot --no-pager
```

Expected: status `active (running)`. If it says `notify timeout`, the sd_notify READY call is failing — verify `sd_notify.ready()` is invoked in `__main__.amain` before `stop_event.wait()`.

- [ ] **Step 3: Commit**

```bash
git add systemd/claude-slack-bot.service
git commit -m "feat(systemd): Type=notify + WatchdogSec=600 (defence-in-depth)"
```

---

## Task 14: Integration test — end to end through worker

This test wires real `Registry`, real `Supervisor`, real `TranscriptReader`, but fake Slack and fake actuator. It writes a JSONL line, pumps the readers, asserts the worker mirrored to Slack with the right uuid dedupe.

**Files:**
- Create: `tests/test_integration_worker.py`

- [ ] **Step 1: Write the test**

```python
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from slackbot.registry import Registry
from slackbot.supervisor import Supervisor


@dataclass
class FakeSlack:
    posts: list[tuple[str, str]] = field(default_factory=list)
    top_level: list[str] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    _ts: int = 0

    def channel_for_agent(self, agent):
        return f"C-{agent.upper()}"

    async def post_top_level(self, text, channel=None):
        self.top_level.append(text)
        self._ts += 1
        return f"top.{self._ts}"

    async def post_in_thread(self, thread_ts, text, channel=None):
        self.posts.append((thread_ts, text))
        self._ts += 1
        return f"thr.{self._ts}"

    async def edit_top_level(self, ts, text, channel=None):
        pass

    async def react(self, ts, emoji, channel=None):
        self.reacts.append((ts, emoji))


class FakeActuator:
    def __init__(self):
        self.deliveries = []

    async def deliver(self, session, pane_id, text):
        self.deliveries.append((session, pane_id, text))


@pytest.mark.asyncio
async def test_full_turn_mirrored_via_transcript(tmp_path: Path, tmp_db_path: str) -> None:
    transcript = tmp_path / "tx.jsonl"
    transcript.write_text("")

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13", agent="claude",
                       slack_channel="C-CLAUDE", transcript_path=str(transcript))
    reg.set_name("s1", "myproj")
    reg.set_thread_ts("s1", "TOP.1")

    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=FakeActuator())
    sup.attach_reader("s1", str(transcript))
    await sup.get_or_create("s1")

    # Simulate CC writing a user message + assistant message to the transcript.
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                             "message": {"role": "user", "content": "hi"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                             "message": {"role": "assistant",
                                         "content": [{"type": "text",
                                                      "text": "hello back"}]}}) + "\n")

    await sup.pump_readers()
    # Give the worker a tick to drain its queue.
    await asyncio.sleep(0.05)
    await sup.shutdown()

    # Both messages mirrored exactly once into the bound thread.
    assert ("TOP.1", "[Claude] 👤 hi") in slack.posts
    assert ("TOP.1", "[Claude] 🤖 hello back") in slack.posts
    assert len(slack.posts) == 2
    reg.close()


@pytest.mark.asyncio
async def test_slack_reply_delivers_and_suppresses_echo(
    tmp_path: Path, tmp_db_path: str
) -> None:
    transcript = tmp_path / "tx.jsonl"
    transcript.write_text("")
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13", agent="claude",
                       slack_channel="C-CLAUDE", transcript_path=str(transcript))
    reg.set_name("s1", "myproj")
    reg.set_thread_ts("s1", "TOP.1")

    slack = FakeSlack()
    actuator = FakeActuator()
    sup = Supervisor(reg=reg, slack=slack, actuator=actuator)
    sup.attach_reader("s1", str(transcript))
    worker = await sup.get_or_create("s1")

    # Slack reply: should deliver to pane and queue an echo suppression.
    await worker.enqueue({"kind": "slack_reply", "text": "ping", "msg_ts": "MSG.1"})
    # Transcript writes the user message a moment later (CC accepted the typed text).
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "user", "uuid": "u_x", "parentUuid": None,
                             "message": {"role": "user", "content": "ping"}}) + "\n")
    await asyncio.sleep(0.05)
    await sup.pump_readers()
    await asyncio.sleep(0.05)
    await sup.shutdown()

    assert actuator.deliveries == [("main", "13", "ping")]
    assert ("MSG.1", "white_check_mark") in slack.reacts
    # The echoed user message must NOT have been mirrored back into Slack.
    assert slack.posts == []
    reg.close()
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_integration_worker.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Lint and commit**

```bash
uv run ruff check tests/
uv run ruff format tests/
git add tests/test_integration_worker.py
git commit -m "test(integration): full turn via transcript reader; slack reply with echo suppress"
```

---

## Task 15: README + migration notes

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Worker redesign — 2026-05-27" section at the bottom**

Insert before the `## Claude Sessions` table:

```markdown
## Worker redesign (2026-05-27)

The daemon now uses a worker-per-conversation model. Each `cc_session_id` owns
an asyncio Task + Queue + per-worker uuid-dedup set. A `TranscriptReader`
tails the JSONL file CC writes; new user/assistant messages flow into the
worker, which decides whether to mirror them to Slack (skipping uuids
already posted, suppressing echoes from Slack-driven deliveries).

A `ReplyRouter` enqueues incoming Slack thread replies into the matching
worker. The global `ZellijActuator` still owns a single `asyncio.Lock` so the
three-call `focus → write-chars → Enter` sequence is atomic across workers.

`session_is_alive` is the only liveness gate. It scans `/proc/*/cmdline`
for the session id (exact argv token) or `--resume <name>` (adjacent argv
tokens), with a 10s TTL cache and the scan offloaded to `asyncio.to_thread`.
The registry's `status` column is diagnostic only.

systemd watchdog: the unit is `Type=notify`; daemon calls `sd_notify`
on startup (`READY=1`), every 60s, and on every received Slack event.
`WatchdogSec=600` so a hung daemon gets restarted within 10 minutes.

After upgrading, restart your CC sessions once so the new `session_start.sh`
runs and writes `transcript_path` into the registry. Existing rows without
`transcript_path` keep working (the transcript reader isn't attached, but
Slack replies still deliver and notifications still mirror).
```

- [ ] **Step 2: Run full test suite + lint**

```bash
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
shellcheck hooks/*.sh
```

Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: worker redesign migration note"
```

---

## Spec Coverage Check

- §1 background → covered by task list (each failure → task)
- §2 goals → tasks 4 (eliminate Stop race), 6 (worker isolation), 2+3 (liveness without staleness), 12 (hook surface reduction)
- §4 architecture → tasks 6 (worker), 7 (supervisor + transcript attach), 11 (main wiring), unchanged actuator
- §5 transcript reader → tasks 4 (reader), 6 (worker consumes), 7 (supervisor attaches)
- §6 liveness → tasks 2 (matcher), 3 (cache), 9 (router uses it), 5 (status drops flip)
- §7 auto-recovery + auto-register → task 8 (handlers do both)
- §8 watchdogs → tasks 10 (sd_notify), 11 (in-process watchdog + heartbeat), 13 (systemd unit)
- §9 hook contract → task 12 (session_start adds transcript_path); prompt.sh/stop.sh remain functional with redundant fields
- §10 event flows → covered by tests in tasks 6, 9, 14
- §11 migration → task 15 README; the redesign is one daemon, hooks are backwards-compat
- §12 testing → tasks 2, 3, 4, 6, 7, 8, 9, 10, 14

Type/name consistency: `Worker` (sid:str), `Supervisor.get_or_create(sid)`, `LivenessCache.is_alive(sid, pid, name)`, `TranscriptReader(path).drain()`, `Registry.upsert_session(..., transcript_path=…)` — consistent across tasks.

No placeholders found.

---

## After execution

Run the README's smoke test (Task 15 → curl events into daemon, post in Slack, verify in pane). Restart your existing named CC sessions so `transcript_path` lands in the registry. The first prompt + response in each session will exercise the new path; the second-chat concurrency case is covered by Task 14's actuator-lock contract.
