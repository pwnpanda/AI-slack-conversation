# claude-slack-bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a long-running daemon that mirrors selected Claude Code sessions into a Slack channel as threads, and types Slack replies back into the originating Zellij pane.

**Architecture:** Single `systemd --user` Python service. Receives CC hook events over `http://127.0.0.1:8787/event`. Holds a persistent Slack Socket Mode connection. Persists session→thread mapping in SQLite. Shells out to `zellij` to deliver Slack replies.

**Tech Stack:** Python 3.13, `uv`, `aiohttp` (HTTP server), `slack_bolt` (async, Socket Mode), `sqlite3` (stdlib), `pytest` + `pytest-asyncio`, bash hooks, `ruff`, `shellcheck`, systemd.

**Spec:** [`docs/superpowers/specs/2026-05-24-claude-slack-bot-design.md`](../specs/2026-05-24-claude-slack-bot-design.md)

**Working directory:** `~/git/priv/claude-slack-bot/`

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/slackbot/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `.python-version`**

```
3.13
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "claude-slack-bot"
version = "0.1.0"
description = "Bridge Claude Code sessions into Slack threads with reply injection via Zellij"
requires-python = ">=3.13,<3.14"
dependencies = [
  "aiohttp==3.11.10",
  "slack-bolt==1.21.2",
  "slack-sdk==3.33.4",
]

[dependency-groups]
dev = [
  "pytest==8.3.4",
  "pytest-asyncio==0.24.0",
  "pytest-aiohttp==1.0.5",
  "ruff==0.8.4",
]

[build-system]
requires = ["uv_build>=0.5"]
build-backend = "uv_build"

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Write `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
*.egg-info/
.env
.coverage
```

- [ ] **Step 4: Write `.env.example`**

```
# Required: Slack credentials. Get from api.slack.com/apps after creating the app.
SLACK_BOT_TOKEN=xoxb-replace-me
SLACK_APP_TOKEN=xapp-replace-me
SLACK_CHANNEL_ID=C0123456789

# Optional: defaults shown
SLACKBOT_PORT=8787
# SLACKBOT_DB_PATH=$XDG_STATE_HOME/claude-slack-bot/registry.db
# SLACKBOT_TMP_DIR=/tmp
CC_SLACK_VERBOSE=off
LOG_LEVEL=INFO
```

- [ ] **Step 5: Create empty package files**

```bash
mkdir -p src/slackbot tests
touch src/slackbot/__init__.py tests/__init__.py
```

- [ ] **Step 6: Write `tests/conftest.py`**

```python
"""Shared pytest fixtures."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def env_full(monkeypatch: pytest.MonkeyPatch, tmp_db_path: str) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C12345")
    monkeypatch.setenv("SLACKBOT_PORT", "0")
    monkeypatch.setenv("SLACKBOT_DB_PATH", tmp_db_path)
    monkeypatch.setenv("SLACKBOT_TMP_DIR", tempfile.gettempdir())
    monkeypatch.setenv("CC_SLACK_VERBOSE", "off")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
```

- [ ] **Step 7: Bootstrap venv and install**

```bash
uv venv && uv sync --all-groups
```

Expected: `.venv/` created, dependencies installed without errors.

- [ ] **Step 8: Run ruff to verify baseline**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both pass.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .python-version .gitignore .env.example src/ tests/
git commit -m "chore: scaffold Python project (3.13, uv, aiohttp, slack-bolt)"
```

---

## Task 2: Config module

**Files:**
- Create: `src/slackbot/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:

```python
import pytest
from slackbot.config import load_config


def test_load_config_full_env(env_full: None) -> None:
    cfg = load_config()
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.slack_app_token == "xapp-test"
    assert cfg.slack_channel_id == "C12345"
    assert cfg.port == 0
    assert cfg.verbose == "off"
    assert cfg.log_level == "WARNING"


def test_load_config_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        load_config()


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "x")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "x")
    for var in ("SLACKBOT_PORT", "CC_SLACK_VERBOSE", "LOG_LEVEL", "SLACKBOT_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.port == 8787
    assert cfg.verbose == "off"
    assert cfg.log_level == "INFO"
    assert cfg.db_path.endswith("/claude-slack-bot/registry.db")
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'slackbot.config'`.

- [ ] **Step 3: Write minimal implementation**

`src/slackbot/config.py`:

```python
"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    slack_channel_id: str
    port: int
    db_path: str
    tmp_dir: str
    verbose: str
    log_level: str


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _default_db_path() -> str:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(state) / "claude-slack-bot" / "registry.db")


def load_config() -> Config:
    return Config(
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_app_token=_require("SLACK_APP_TOKEN"),
        slack_channel_id=_require("SLACK_CHANNEL_ID"),
        port=int(os.environ.get("SLACKBOT_PORT", "8787")),
        db_path=os.environ.get("SLACKBOT_DB_PATH") or _default_db_path(),
        tmp_dir=os.environ.get("SLACKBOT_TMP_DIR", "/tmp"),
        verbose=os.environ.get("CC_SLACK_VERBOSE", "off"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Lint**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/slackbot/config.py tests/test_config.py
git commit -m "feat(config): env-driven Config dataclass with required/optional vars"
```

---

## Task 3: Registry — sessions table

**Files:**
- Create: `src/slackbot/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

`tests/test_registry.py`:

```python
from pathlib import Path

from slackbot.registry import Registry


def test_open_creates_schema(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    assert Path(tmp_db_path).exists()
    reg.close()


def test_upsert_session_inserts_when_missing(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session(
        cc_session_id="abc",
        cwd="/home/r/p",
        zellij_session="default",
        zellij_pane_id="0",
    )
    sess = reg.get_session("abc")
    assert sess is not None
    assert sess.cc_session_id == "abc"
    assert sess.cwd == "/home/r/p"
    assert sess.zellij_session == "default"
    assert sess.zellij_pane_id == "0"
    assert sess.name is None
    assert sess.status == "active"
    assert sess.slack_thread_ts is None
    reg.close()


def test_upsert_session_updates_zellij_on_resume(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("abc", "/x", "s1", "p1")
    reg.set_name("abc", "myname")
    reg.set_thread_ts("abc", "1111.2222")
    reg.set_status("abc", "ended")
    reg.upsert_session("abc", "/x", "s2", "p2")  # resume
    sess = reg.get_session("abc")
    assert sess is not None
    assert sess.name == "myname"
    assert sess.slack_thread_ts == "1111.2222"
    assert sess.zellij_session == "s2"
    assert sess.zellij_pane_id == "p2"
    assert sess.status == "active"
    reg.close()


def test_claim_name_returns_prior_holder(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("old", "/x", "s", "p1")
    reg.set_name("old", "shared")
    reg.set_thread_ts("old", "111.222")
    reg.upsert_session("new", "/x", "s", "p2")
    prior_thread = reg.claim_name("new", "shared")
    assert prior_thread == "111.222"
    old_sess = reg.get_session("old")
    assert old_sess is not None and old_sess.name is None
    new_sess = reg.get_session("new")
    assert new_sess is not None
    assert new_sess.name == "shared"
    assert new_sess.slack_thread_ts == "111.222"
    reg.close()


def test_set_status_updates_value(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("abc", "/x", "s", "p")
    reg.set_status("abc", "ended")
    sess = reg.get_session("abc")
    assert sess is not None and sess.status == "ended"
    reg.close()


def test_get_session_missing_returns_none(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    assert reg.get_session("nope") is None
    reg.close()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_registry.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`src/slackbot/registry.py`:

```python
"""SQLite-backed session and event registry. Single-writer, sync."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  cc_session_id   TEXT PRIMARY KEY,
  name            TEXT,
  cwd             TEXT NOT NULL,
  zellij_session  TEXT,
  zellij_pane_id  TEXT,
  slack_channel   TEXT,
  slack_thread_ts TEXT,
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


@dataclass(frozen=True)
class Session:
    cc_session_id: str
    name: str | None
    cwd: str
    zellij_session: str | None
    zellij_pane_id: str | None
    slack_channel: str | None
    slack_thread_ts: str | None
    created_at: int
    last_event_at: int
    status: str


class Registry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Registry not opened")
        return self._conn

    def upsert_session(
        self,
        cc_session_id: str,
        cwd: str,
        zellij_session: str | None,
        zellij_pane_id: str | None,
    ) -> None:
        now = int(time.time())
        self._c().execute(
            """
            INSERT INTO sessions (cc_session_id, cwd, zellij_session, zellij_pane_id,
                                  created_at, last_event_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(cc_session_id) DO UPDATE SET
              cwd = excluded.cwd,
              zellij_session = excluded.zellij_session,
              zellij_pane_id = excluded.zellij_pane_id,
              last_event_at = excluded.last_event_at,
              status = 'active'
            """,
            (cc_session_id, cwd, zellij_session, zellij_pane_id, now, now),
        )

    def get_session(self, cc_session_id: str) -> Session | None:
        row = self._c().execute(
            "SELECT * FROM sessions WHERE cc_session_id = ?", (cc_session_id,)
        ).fetchone()
        return _row_to_session(row) if row else None

    def get_session_by_thread(self, thread_ts: str) -> Session | None:
        row = self._c().execute(
            "SELECT * FROM sessions WHERE slack_thread_ts = ?", (thread_ts,)
        ).fetchone()
        return _row_to_session(row) if row else None

    def set_name(self, cc_session_id: str, name: str) -> None:
        self._c().execute(
            "UPDATE sessions SET name = ?, last_event_at = ? WHERE cc_session_id = ?",
            (name, int(time.time()), cc_session_id),
        )

    def clear_name(self, cc_session_id: str) -> None:
        self._c().execute(
            "UPDATE sessions SET name = NULL WHERE cc_session_id = ?", (cc_session_id,)
        )

    def set_thread_ts(self, cc_session_id: str, thread_ts: str) -> None:
        self._c().execute(
            "UPDATE sessions SET slack_thread_ts = ? WHERE cc_session_id = ?",
            (thread_ts, cc_session_id),
        )

    def set_status(self, cc_session_id: str, status: str) -> None:
        self._c().execute(
            "UPDATE sessions SET status = ?, last_event_at = ? WHERE cc_session_id = ?",
            (status, int(time.time()), cc_session_id),
        )

    def claim_name(self, cc_session_id: str, name: str) -> str | None:
        """Claim `name` for `cc_session_id`. Returns prior holder's thread_ts (or None)."""
        prior = self._c().execute(
            "SELECT cc_session_id, slack_thread_ts FROM sessions "
            "WHERE name = ? AND cc_session_id != ?",
            (name, cc_session_id),
        ).fetchone()
        prior_thread: str | None = prior["slack_thread_ts"] if prior else None
        if prior:
            self.clear_name(prior["cc_session_id"])
        self.set_name(cc_session_id, name)
        if prior_thread:
            self.set_thread_ts(cc_session_id, prior_thread)
        return prior_thread


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        cc_session_id=row["cc_session_id"],
        name=row["name"],
        cwd=row["cwd"],
        zellij_session=row["zellij_session"],
        zellij_pane_id=row["zellij_pane_id"],
        slack_channel=row["slack_channel"],
        slack_thread_ts=row["slack_thread_ts"],
        created_at=row["created_at"],
        last_event_at=row["last_event_at"],
        status=row["status"],
    )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_registry.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/registry.py tests/test_registry.py
git commit -m "feat(registry): SQLite sessions table with upsert/name-claim semantics"
```

---

## Task 4: Registry — event_log buffering

**Files:**
- Modify: `src/slackbot/registry.py` (add Event dataclass + 3 methods)
- Modify: `tests/test_registry.py` (append 2 tests)

- [ ] **Step 1: Append to `tests/test_registry.py`**

```python
def test_buffer_and_drain_events(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("abc", "/x", "s", "p")
    reg.buffer_event("abc", "prompt", '{"text":"hello"}')
    reg.buffer_event("abc", "response", '{"text":"hi"}')
    pending = reg.drain_unposted("abc")
    assert len(pending) == 2
    assert pending[0].kind == "prompt"
    assert pending[1].kind == "response"
    for ev in pending:
        reg.mark_event_posted(ev.id, "1.0")
    assert reg.drain_unposted("abc") == []
    reg.close()


def test_buffer_event_preserves_order(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("abc", "/x", "s", "p")
    for i in range(5):
        reg.buffer_event("abc", "prompt", f'{{"i":{i}}}')
    events = reg.drain_unposted("abc")
    assert [e.payload for e in events] == [f'{{"i":{i}}}' for i in range(5)]
    reg.close()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_registry.py::test_buffer_and_drain_events -v
```

Expected: FAIL with `AttributeError: ... 'buffer_event'`.

- [ ] **Step 3: Add to `src/slackbot/registry.py`**

Add this dataclass after `Session`:

```python
@dataclass(frozen=True)
class Event:
    id: int
    cc_session_id: str
    ts: int
    kind: str
    payload: str
    slack_msg_ts: str | None
```

Add these methods to the `Registry` class:

```python
    def buffer_event(self, cc_session_id: str, kind: str, payload: str) -> int:
        cur = self._c().execute(
            "INSERT INTO event_log (cc_session_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (cc_session_id, int(time.time()), kind, payload),
        )
        return cur.lastrowid or 0

    def drain_unposted(self, cc_session_id: str) -> list[Event]:
        rows = self._c().execute(
            "SELECT id, cc_session_id, ts, kind, payload, slack_msg_ts FROM event_log "
            "WHERE cc_session_id = ? AND slack_msg_ts IS NULL ORDER BY id ASC",
            (cc_session_id,),
        ).fetchall()
        return [
            Event(
                id=r["id"],
                cc_session_id=r["cc_session_id"],
                ts=r["ts"],
                kind=r["kind"],
                payload=r["payload"],
                slack_msg_ts=r["slack_msg_ts"],
            )
            for r in rows
        ]

    def mark_event_posted(self, event_id: int, slack_msg_ts: str) -> None:
        self._c().execute(
            "UPDATE event_log SET slack_msg_ts = ? WHERE id = ?",
            (slack_msg_ts, event_id),
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_registry.py -v
```

Expected: 8 passed total.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/registry.py tests/test_registry.py
git commit -m "feat(registry): event_log buffer/drain/mark-posted for pre-name buffering"
```

---

## Task 5: Events module — payload → Slack-text formatter

**Files:**
- Create: `src/slackbot/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

`tests/test_events.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_events.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

`src/slackbot/events.py`:

```python
"""Pure functions that turn structured events into Slack message text."""
from __future__ import annotations

import re
from typing import Any

_RN_PATTERN = re.compile(r"^/(?:rn|rename|register)\s+(\S+)\s*$")
_TRUNCATE_AT = 3000


def top_level_text(name: str, cwd: str, status: str) -> str:
    if status == "active":
        return f"🟢 {name}  ·  {cwd}"
    return f"⚪ {name}  ·  {cwd}  (ended)"


def _codeblock_if_multiline(text: str) -> str:
    if "\n" in text:
        truncated = text if len(text) <= _TRUNCATE_AT else text[:_TRUNCATE_AT] + "\n…[truncated]"
        return f"\n```\n{truncated}\n```"
    return f" {text}"


def format_event(kind: str, data: dict[str, Any]) -> str:
    if kind == "prompt":
        return f"👤{_codeblock_if_multiline(str(data.get('text', '')))}"
    if kind == "response":
        body = f"🤖{_codeblock_if_multiline(str(data.get('text', '')))}"
        summary = data.get("tool_summary")
        if summary:
            body += f"\n_↳ {summary}_"
        return body
    if kind == "notification":
        return f"⏸ {data.get('message', '')}"
    if kind == "error":
        return f"❌ {data.get('text', '')}"
    return f"[{kind}] {data!r}"


def parse_rn_command(prompt_text: str) -> str | None:
    """Return the name argument if `prompt_text` is an /rn-style command."""
    m = _RN_PATTERN.match(prompt_text.strip())
    return m.group(1) if m else None
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_events.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/events.py tests/test_events.py
git commit -m "feat(events): pure formatters for Slack message text and rn parser"
```

---

## Task 6: Slack IO — async wrapper

**Files:**
- Create: `src/slackbot/slack_io.py`
- Test: `tests/test_slack_io.py`

- [ ] **Step 1: Write the failing test**

`tests/test_slack_io.py`:

```python
from dataclasses import dataclass, field

import pytest

from slackbot.slack_io import SlackIO


@dataclass
class FakeSlackClient:
    posted: list[dict] = field(default_factory=list)
    edited: list[dict] = field(default_factory=list)
    reacted: list[dict] = field(default_factory=list)
    next_ts: str = "1700000000.000001"

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": self.next_ts}

    async def chat_update(self, **kwargs):
        self.edited.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs):
        self.reacted.append(kwargs)
        return {"ok": True}


@pytest.mark.asyncio
async def test_post_top_level_returns_ts() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    ts = await io.post_top_level("🟢 myproj")
    assert ts == fake.next_ts
    assert fake.posted[0] == {"channel": "C1", "text": "🟢 myproj"}


@pytest.mark.asyncio
async def test_post_in_thread_uses_thread_ts() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    await io.post_in_thread("1.0", "👤 hi")
    assert fake.posted[0] == {"channel": "C1", "text": "👤 hi", "thread_ts": "1.0"}


@pytest.mark.asyncio
async def test_edit_top_level_calls_update() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    await io.edit_top_level("1.0", "⚪ done")
    assert fake.edited[0] == {"channel": "C1", "ts": "1.0", "text": "⚪ done"}


@pytest.mark.asyncio
async def test_react_calls_reactions_add() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    await io.react("1.0", "white_check_mark")
    assert fake.reacted[0] == {
        "channel": "C1",
        "timestamp": "1.0",
        "name": "white_check_mark",
    }
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_slack_io.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

`src/slackbot/slack_io.py`:

```python
"""Thin async wrapper over slack_sdk's AsyncWebClient for channel/thread ops."""
from __future__ import annotations

from typing import Protocol


class _AsyncSlackClient(Protocol):
    async def chat_postMessage(self, **kwargs: object) -> dict: ...
    async def chat_update(self, **kwargs: object) -> dict: ...
    async def reactions_add(self, **kwargs: object) -> dict: ...


class SlackIO:
    def __init__(self, client: _AsyncSlackClient, channel: str) -> None:
        self._client = client
        self._channel = channel

    async def post_top_level(self, text: str) -> str:
        resp = await self._client.chat_postMessage(channel=self._channel, text=text)
        return str(resp["ts"])

    async def post_in_thread(self, thread_ts: str, text: str) -> str:
        resp = await self._client.chat_postMessage(
            channel=self._channel, text=text, thread_ts=thread_ts
        )
        return str(resp["ts"])

    async def edit_top_level(self, ts: str, text: str) -> None:
        await self._client.chat_update(channel=self._channel, ts=ts, text=text)

    async def react(self, ts: str, emoji: str) -> None:
        await self._client.reactions_add(
            channel=self._channel, timestamp=ts, name=emoji
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_slack_io.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/slack_io.py tests/test_slack_io.py
git commit -m "feat(slack_io): async wrapper for postMessage/update/reactions"
```

---

## Task 7: Zellij actuator

Shells out to `zellij` to focus a pane, write text, then send Enter (keycode 13). Uses `subprocess.run` via `asyncio.to_thread` for safety — never invokes a shell, always uses an argument list.

**Files:**
- Create: `src/slackbot/zellij_io.py`
- Test: `tests/test_zellij_io.py`

- [ ] **Step 1: Write the failing test**

`tests/test_zellij_io.py`:

```python
import os
import stat
from pathlib import Path

import pytest

from slackbot.zellij_io import ZellijActuator, ZellijError


def _make_fake_zellij(bin_dir: Path, *, exit_code: int = 0, log_file: Path | None = None) -> None:
    script = bin_dir / "zellij"
    log_path = str(log_file) if log_file else "/dev/null"
    script.write_text(
        f"""#!/usr/bin/env bash
echo "$@" >> {log_path}
exit {exit_code}
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.asyncio
async def test_deliver_calls_focus_then_write_then_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    _make_fake_zellij(bin_dir, log_file=log_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    act = ZellijActuator()
    await act.deliver(session="main", pane_id="0", text="hello world")

    calls = log_file.read_text().splitlines()
    assert len(calls) == 3
    assert "--session main action focus-pane-with-id 0" in calls[0]
    assert "--session main action write-chars hello world" in calls[1]
    assert "--session main action write 13" in calls[2]


@pytest.mark.asyncio
async def test_deliver_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_zellij(bin_dir, exit_code=2)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    act = ZellijActuator()
    with pytest.raises(ZellijError):
        await act.deliver(session="main", pane_id="0", text="hi")
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_zellij_io.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

`src/slackbot/zellij_io.py`:

```python
"""Drives the host's zellij multiplexer via subprocess calls (no shell)."""
from __future__ import annotations

import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)


class ZellijError(RuntimeError):
    pass


class ZellijActuator:
    async def deliver(self, session: str, pane_id: str, text: str) -> None:
        await self._zellij(session, "action", "focus-pane-with-id", pane_id)
        await self._zellij(session, "action", "write-chars", text)
        await self._zellij(session, "action", "write", "13")

    async def _zellij(self, session: str, *args: str) -> None:
        cmd = ["zellij", "--session", session, *args]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise ZellijError(
                f"zellij {' '.join(args)} -> {result.returncode}: {result.stderr.strip()}"
            )
        log.debug("zellij ok: %s", " ".join(args))
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_zellij_io.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/zellij_io.py tests/test_zellij_io.py
git commit -m "feat(zellij_io): subprocess actuator for focus + write-chars + Enter"
```

---

## Task 8: Event handlers (the brain)

Wires registry + slack_io together. The class has one async method per `kind`.

**Files:**
- Create: `src/slackbot/handlers.py`
- Test: `tests/test_handlers.py`

- [ ] **Step 1: Write the failing test**

`tests/test_handlers.py`:

```python
from dataclasses import dataclass, field

import pytest

from slackbot.handlers import EventHandlers
from slackbot.registry import Registry


@dataclass
class FakeSlackIO:
    top_level_posts: list[str] = field(default_factory=list)
    thread_posts: list[tuple[str, str]] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    _ts_counter: int = 0

    async def post_top_level(self, text: str) -> str:
        self.top_level_posts.append(text)
        self._ts_counter += 1
        return f"top.{self._ts_counter}"

    async def post_in_thread(self, thread_ts: str, text: str) -> str:
        self.thread_posts.append((thread_ts, text))
        self._ts_counter += 1
        return f"thr.{self._ts_counter}"

    async def edit_top_level(self, ts: str, text: str) -> None:
        self.edits.append((ts, text))

    async def react(self, ts: str, emoji: str) -> None:
        self.reacts.append((ts, emoji))


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


def _start(sid: str = "s1", cwd: str = "/x", pane: str = "0") -> dict:
    return {
        "kind": "start",
        "session_id": sid,
        "cwd": cwd,
        "zellij_session": "main",
        "zellij_pane_id": pane,
        "resumed": False,
    }


@pytest.mark.asyncio
async def test_start_event_inserts_no_post_without_name(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    assert slack.top_level_posts == []
    assert reg.get_session("s1") is not None


@pytest.mark.asyncio
async def test_prompt_before_name_is_buffered(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "hello"})
    assert slack.thread_posts == []
    buffered = reg.drain_unposted("s1")
    assert len(buffered) == 1
    assert buffered[0].kind == "prompt"


@pytest.mark.asyncio
async def test_name_event_posts_top_level_and_drains_buffer(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "before-name"})
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})

    assert len(slack.top_level_posts) == 1
    assert slack.top_level_posts[0] == "🟢 myproj  ·  /x"
    assert len(slack.thread_posts) == 1
    assert slack.thread_posts[0][1] == "👤 before-name"
    assert reg.drain_unposted("s1") == []


@pytest.mark.asyncio
async def test_prompt_after_name_posts_directly(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "live"})
    assert len(slack.thread_posts) == 1
    assert slack.thread_posts[0][1] == "👤 live"


@pytest.mark.asyncio
async def test_rename_edits_top_level(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "old"})
    await h.handle({"kind": "name", "session_id": "s1", "name": "new"})
    assert len(slack.top_level_posts) == 1
    assert slack.edits[-1][1] == "🟢 new  ·  /x"


@pytest.mark.asyncio
async def test_end_event_edits_to_ended(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "done"})
    assert slack.edits[-1][1] == "⚪ myproj  ·  /x  (ended)"
    sess = reg.get_session("s1")
    assert sess is not None and sess.status == "ended"


@pytest.mark.asyncio
async def test_name_reclaim_posts_divider(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start("s1"))
    await h.handle({"kind": "name", "session_id": "s1", "name": "shared"})
    await h.handle(_start("s2", pane="1"))
    await h.handle({"kind": "name", "session_id": "s2", "name": "shared"})

    assert len(slack.top_level_posts) == 1
    divider_posts = [t for _, t in slack.thread_posts if "resumed" in t]
    assert len(divider_posts) == 1


@pytest.mark.asyncio
async def test_resumed_start_flips_back_to_active(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "x"})
    # resume: same session_id, new pane id
    resumed = _start(pane="2")
    resumed["resumed"] = True
    await h.handle(resumed)
    assert slack.edits[-1][1] == "🟢 myproj  ·  /x"
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_handlers.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

`src/slackbot/handlers.py`:

```python
"""Wires hook events through the registry to Slack."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from slackbot.events import format_event, top_level_text
from slackbot.registry import Registry, Session

log = logging.getLogger(__name__)


class _SlackIOProto(Protocol):
    async def post_top_level(self, text: str) -> str: ...
    async def post_in_thread(self, thread_ts: str, text: str) -> str: ...
    async def edit_top_level(self, ts: str, text: str) -> None: ...
    async def react(self, ts: str, emoji: str) -> None: ...


class EventHandlers:
    def __init__(self, reg: Registry, slack: _SlackIOProto) -> None:
        self._reg = reg
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

    async def _on_start(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        prior = self._reg.get_session(sid)
        self._reg.upsert_session(
            sid, ev["cwd"], ev.get("zellij_session"), ev.get("zellij_pane_id")
        )
        if prior and prior.name and prior.slack_thread_ts:
            await self._slack.edit_top_level(
                prior.slack_thread_ts,
                top_level_text(prior.name, prior.cwd, "active"),
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

        if sess.name is None:
            prior_thread = self._reg.claim_name(sid, new_name)
            sess = self._reg.get_session(sid)
            assert sess is not None
            if prior_thread:
                await self._slack.post_in_thread(
                    prior_thread,
                    f"─── 🔄 resumed in new session @ {_iso_now()} ───",
                )
            else:
                ts = await self._slack.post_top_level(
                    top_level_text(new_name, sess.cwd, "active")
                )
                self._reg.set_thread_ts(sid, ts)
                sess = self._reg.get_session(sid)
                assert sess is not None
            await self._drain_buffer(sess)
        else:
            self._reg.set_name(sid, new_name)
            if sess.slack_thread_ts:
                await self._slack.edit_top_level(
                    sess.slack_thread_ts,
                    top_level_text(new_name, sess.cwd, sess.status),
                )

    async def _on_prompt(self, ev: dict[str, Any]) -> None:
        await self._post_or_buffer(
            ev["session_id"], "prompt", {"text": ev.get("text", "")}
        )

    async def _on_response(self, ev: dict[str, Any]) -> None:
        data = {"text": ev.get("text", ""), "tool_summary": ev.get("tool_summary")}
        await self._post_or_buffer(ev["session_id"], "response", data)

    async def _on_notification(self, ev: dict[str, Any]) -> None:
        await self._post_or_buffer(
            ev["session_id"], "notification", {"message": ev.get("message", "")}
        )

    async def _on_error(self, ev: dict[str, Any]) -> None:
        await self._post_or_buffer(
            ev["session_id"], "error", {"text": ev.get("text", "")}
        )

    async def _on_end(self, ev: dict[str, Any]) -> None:
        sid = ev["session_id"]
        self._reg.set_status(sid, "ended")
        sess = self._reg.get_session(sid)
        if sess and sess.name and sess.slack_thread_ts:
            await self._slack.edit_top_level(
                sess.slack_thread_ts, top_level_text(sess.name, sess.cwd, "ended")
            )

    async def _post_or_buffer(
        self, sid: str, kind: str, data: dict[str, Any]
    ) -> None:
        sess = self._reg.get_session(sid)
        if sess is None:
            log.warning("event for unknown session %s", sid)
            return
        payload = json.dumps(data)
        if sess.name is None or sess.slack_thread_ts is None:
            self._reg.buffer_event(sid, kind, payload)
            return
        text = format_event(kind, data)
        ts = await self._slack.post_in_thread(sess.slack_thread_ts, text)
        evt_id = self._reg.buffer_event(sid, kind, payload)
        self._reg.mark_event_posted(evt_id, ts)

    async def _drain_buffer(self, sess: Session) -> None:
        assert sess.slack_thread_ts is not None
        for ev in self._reg.drain_unposted(sess.cc_session_id):
            data = json.loads(ev.payload)
            ts = await self._slack.post_in_thread(
                sess.slack_thread_ts, format_event(ev.kind, data)
            )
            self._reg.mark_event_posted(ev.id, ts)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_handlers.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/handlers.py tests/test_handlers.py
git commit -m "feat(handlers): wire events through registry to Slack with buffering"
```

---

## Task 9: Reply router (Slack → Zellij)

**Files:**
- Create: `src/slackbot/reply_router.py`
- Test: `tests/test_reply_router.py`

- [ ] **Step 1: Write the failing test**

`tests/test_reply_router.py`:

```python
from dataclasses import dataclass, field

import pytest

from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.zellij_io import ZellijError


@dataclass
class FakeActuator:
    deliveries: list[tuple[str, str, str]] = field(default_factory=list)
    fail: bool = False

    async def deliver(self, session: str, pane_id: str, text: str) -> None:
        if self.fail:
            raise ZellijError("boom")
        self.deliveries.append((session, pane_id, text))


@dataclass
class FakeSlackIO:
    thread_posts: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)

    async def post_in_thread(self, thread_ts: str, text: str) -> str:
        self.thread_posts.append((thread_ts, text))
        return "x.x"

    async def react(self, ts: str, emoji: str) -> None:
        self.reacts.append((ts, emoji))


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


@pytest.mark.asyncio
async def test_reply_routes_to_correct_pane(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3")
    reg.set_name("s1", "myproj")
    reg.set_thread_ts("s1", "TOP.1")
    actuator = FakeActuator()
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack)
    await router.on_reply(thread_ts="TOP.1", text="do X", msg_ts="MSG.1")
    assert actuator.deliveries == [("main", "3", "do X")]
    assert ("MSG.1", "white_check_mark") in slack.reacts


@pytest.mark.asyncio
async def test_reply_for_unknown_thread_ignored(reg: Registry) -> None:
    actuator = FakeActuator()
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack)
    await router.on_reply(thread_ts="UNKNOWN", text="x", msg_ts="MSG.1")
    assert actuator.deliveries == []
    assert slack.reacts == []


@pytest.mark.asyncio
async def test_reply_to_ended_session_posts_offline_warning(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    reg.set_status("s1", "ended")
    actuator = FakeActuator()
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack)
    await router.on_reply(thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert actuator.deliveries == []
    assert any("offline" in t for _, t in slack.thread_posts)
    assert ("MSG.1", "no_entry_sign") in slack.reacts


@pytest.mark.asyncio
async def test_reply_failure_posts_error_and_reacts_warn(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    actuator = FakeActuator(fail=True)
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack)
    await router.on_reply(thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert any("delivery failed" in t for _, t in slack.thread_posts)
    assert ("MSG.1", "warning") in slack.reacts
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_reply_router.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

`src/slackbot/reply_router.py`:

```python
"""Routes Slack thread replies to the originating Zellij pane."""
from __future__ import annotations

import logging
from typing import Protocol

from slackbot.registry import Registry
from slackbot.zellij_io import ZellijError

log = logging.getLogger(__name__)


class _ActuatorProto(Protocol):
    async def deliver(self, session: str, pane_id: str, text: str) -> None: ...


class _SlackIOProto(Protocol):
    async def post_in_thread(self, thread_ts: str, text: str) -> str: ...
    async def react(self, ts: str, emoji: str) -> None: ...


class ReplyRouter:
    def __init__(
        self, reg: Registry, actuator: _ActuatorProto, slack: _SlackIOProto
    ) -> None:
        self._reg = reg
        self._actuator = actuator
        self._slack = slack

    async def on_reply(self, thread_ts: str, text: str, msg_ts: str) -> None:
        sess = self._reg.get_session_by_thread(thread_ts)
        if sess is None:
            log.debug("reply for unknown thread %s ignored", thread_ts)
            return

        if sess.status == "ended":
            await self._slack.post_in_thread(
                thread_ts, "⚠️ session offline, reply not sent"
            )
            await self._slack.react(msg_ts, "no_entry_sign")
            return

        if not sess.zellij_session or not sess.zellij_pane_id:
            await self._slack.post_in_thread(
                thread_ts, "❌ delivery failed: session has no pane info"
            )
            await self._slack.react(msg_ts, "warning")
            return

        try:
            await self._actuator.deliver(
                sess.zellij_session, sess.zellij_pane_id, text
            )
        except ZellijError as exc:
            await self._slack.post_in_thread(
                thread_ts, f"❌ delivery failed: {exc}"
            )
            await self._slack.react(msg_ts, "warning")
            return

        await self._slack.react(msg_ts, "white_check_mark")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_reply_router.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/reply_router.py tests/test_reply_router.py
git commit -m "feat(reply_router): route Slack thread replies to zellij panes"
```

---

## Task 10: HTTP event endpoint

**Files:**
- Create: `src/slackbot/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

`tests/test_server.py`:

```python
from dataclasses import dataclass, field

import pytest

from slackbot.server import make_app


@dataclass
class FakeHandlers:
    received: list[dict] = field(default_factory=list)
    raise_exc: Exception | None = None

    async def handle(self, event: dict) -> None:
        if self.raise_exc:
            raise self.raise_exc
        self.received.append(event)


@pytest.mark.asyncio
async def test_post_event_dispatches_to_handlers(aiohttp_client) -> None:
    handlers = FakeHandlers()
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/event",
        json={"v": 1, "kind": "prompt", "session_id": "s1", "text": "hi"},
    )
    assert resp.status == 204
    assert handlers.received == [
        {"v": 1, "kind": "prompt", "session_id": "s1", "text": "hi"}
    ]


@pytest.mark.asyncio
async def test_post_event_returns_400_on_invalid_json(aiohttp_client) -> None:
    handlers = FakeHandlers()
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.post("/event", data="not-json")
    assert resp.status == 400


@pytest.mark.asyncio
async def test_post_event_returns_500_on_handler_error(aiohttp_client) -> None:
    handlers = FakeHandlers(raise_exc=RuntimeError("boom"))
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.post("/event", json={"v": 1, "kind": "x", "session_id": "s"})
    assert resp.status == 500


@pytest.mark.asyncio
async def test_get_healthz(aiohttp_client) -> None:
    handlers = FakeHandlers()
    app = make_app(handlers)
    client = await aiohttp_client(app)
    resp = await client.get("/healthz")
    assert resp.status == 200
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_server.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

`src/slackbot/server.py`:

```python
"""aiohttp HTTP server: receives CC hook events on POST /event."""
from __future__ import annotations

import json
import logging
from typing import Protocol

from aiohttp import web

log = logging.getLogger(__name__)


class _HandlersProto(Protocol):
    async def handle(self, event: dict) -> None: ...


def make_app(handlers: _HandlersProto) -> web.Application:
    app = web.Application()

    async def post_event(request: web.Request) -> web.Response:
        try:
            body = await request.text()
            event = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return web.Response(status=400, text="invalid json")
        try:
            await handlers.handle(event)
        except Exception:
            log.exception("handler error for event=%r", event)
            return web.Response(status=500, text="handler error")
        return web.Response(status=204)

    async def healthz(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_post("/event", post_event)
    app.router.add_get("/healthz", healthz)
    return app
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_server.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
git add src/slackbot/server.py tests/test_server.py
git commit -m "feat(server): aiohttp /event endpoint for CC hooks"
```

---

## Task 11: Logging setup + main entry point

**Files:**
- Create: `src/slackbot/logging_setup.py`
- Create: `src/slackbot/__main__.py`

- [ ] **Step 1: Write `src/slackbot/logging_setup.py`**

```python
"""Configure root logger from LOG_LEVEL."""
from __future__ import annotations

import logging
import sys


def configure(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
```

- [ ] **Step 2: Write `src/slackbot/__main__.py`**

```python
"""Daemon entry point. Runs aiohttp event server + Slack Socket Mode together."""
from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from slackbot.config import load_config
from slackbot.handlers import EventHandlers
from slackbot.logging_setup import configure as configure_logging
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.server import make_app
from slackbot.slack_io import SlackIO
from slackbot.zellij_io import ZellijActuator

log = logging.getLogger("slackbot.main")


async def amain() -> None:
    cfg = load_config()
    configure_logging(cfg.log_level)
    log.info(
        "starting claude-slack-bot port=%d channel=%s",
        cfg.port,
        cfg.slack_channel_id,
    )

    reg = Registry(cfg.db_path)
    reg.open()

    web_client = AsyncWebClient(token=cfg.slack_bot_token)
    slack_io = SlackIO(web_client, cfg.slack_channel_id)
    handlers = EventHandlers(reg, slack_io)
    actuator = ZellijActuator()
    router = ReplyRouter(reg, actuator, slack_io)

    bolt = AsyncApp(token=cfg.slack_bot_token, client=web_client)

    @bolt.event("message")
    async def on_message(event, logger):  # noqa: ARG001
        if event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        text = event.get("text", "")
        msg_ts = event.get("ts", "")
        await router.on_reply(thread_ts=thread_ts, text=text, msg_ts=msg_ts)

    socket_handler = AsyncSocketModeHandler(bolt, cfg.slack_app_token)
    http_app = make_app(handlers)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    http_site = web.TCPSite(http_runner, "127.0.0.1", cfg.port)
    await http_site.start()
    log.info("http event endpoint listening on 127.0.0.1:%d", cfg.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    socket_task = asyncio.create_task(socket_handler.start_async())

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        socket_task.cancel()
        await http_runner.cleanup()
        reg.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify imports succeed**

```bash
uv run python -c "from slackbot.__main__ import main; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/ && uv run ruff format src/
git add src/slackbot/logging_setup.py src/slackbot/__main__.py
git commit -m "feat: main entry point wiring HTTP + Slack Socket Mode + handlers"
```

---

## Task 12: Hook scripts

Five bash hooks that POST events to the daemon. CC passes hook input on stdin as JSON; field names (`session_id`, `cwd`, `prompt`, `message`, `transcript_path`, `reason`) follow the current CC hook contract — verify against `claude --help` if a field name has changed.

**Files:**
- Create: `hooks/session_start.sh`
- Create: `hooks/prompt.sh`
- Create: `hooks/stop.sh`
- Create: `hooks/notify.sh`
- Create: `hooks/session_end.sh`

- [ ] **Step 1: Write `hooks/session_start.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0
resumed_flag=false
case "${CLAUDE_HOOK_SOURCE:-}" in
  resume|*resume*) resumed_flag=true ;;
esac
payload="$(jq -n \
  --arg sid "$sid" \
  --arg cwd "${cwd:-$PWD}" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" \
  --arg zp "${ZELLIJ_PANE_ID:-}" \
  --argjson resumed "$resumed_flag" \
  '{v:1,kind:"start",session_id:$sid,cwd:$cwd,zellij_session:$zs,zellij_pane_id:$zp,resumed:$resumed}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
```

- [ ] **Step 2: Write `hooks/prompt.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0

post() {
  curl -fsS --max-time 1 -H 'content-type: application/json' \
    -d "$1" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
}

prompt_payload="$(jq -n --arg sid "$sid" --arg t "$prompt" \
  '{v:1,kind:"prompt",session_id:$sid,text:$t}')"
post "$prompt_payload"

# Detect /rn-style commands and emit a name event
name="$(printf '%s' "$prompt" \
  | sed -n -E 's@^/(rn|rename|register)[[:space:]]+([^[:space:]]+)[[:space:]]*$@\2@p')"
if [ -n "$name" ]; then
  name_payload="$(jq -n --arg sid "$sid" --arg n "$name" \
    '{v:1,kind:"name",session_id:$sid,name:$n}')"
  post "$name_payload"
fi
exit 0
```

- [ ] **Step 3: Write `hooks/stop.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0

# Best-effort: pull last assistant message from transcript_path if available
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
last_text=""
if [ -n "$transcript" ] && [ -r "$transcript" ]; then
  last_text="$(tac "$transcript" 2>/dev/null \
    | jq -r 'select(.message.role=="assistant") | .message.content // empty' 2>/dev/null \
    | grep -v '^$' \
    | head -n 1 || true)"
fi

payload="$(jq -n --arg sid "$sid" --arg t "$last_text" \
  '{v:1,kind:"response",session_id:$sid,text:$t}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
```

- [ ] **Step 4: Write `hooks/notify.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
msg="$(printf '%s' "$input" | jq -r '.message // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0
payload="$(jq -n --arg sid "$sid" --arg m "$msg" \
  '{v:1,kind:"notification",session_id:$sid,message:$m}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
```

- [ ] **Step 5: Write `hooks/session_end.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
reason="$(printf '%s' "$input" | jq -r '.reason // "unknown"' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0
payload="$(jq -n --arg sid "$sid" --arg r "$reason" \
  '{v:1,kind:"end",session_id:$sid,reason:$r}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
```

- [ ] **Step 6: Make executable and shellcheck**

```bash
chmod +x hooks/*.sh
shellcheck hooks/*.sh
```

Expected: shellcheck reports no issues.

- [ ] **Step 7: Commit**

```bash
git add hooks/
git commit -m "feat(hooks): bash scripts emitting structured events to daemon"
```

---

## Task 13: Hook installer

**Files:**
- Create: `hooks/install.sh`
- Test: `tests/test_install.py`

- [ ] **Step 1: Write the failing test**

`tests/test_install.py`:

```python
import json
import subprocess
from pathlib import Path


def _run_install(repo_root: Path, claude_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo_root / "hooks" / "install.sh")],
        env={"HOME": str(claude_home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
    )


def test_install_creates_settings_with_hooks(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    claude_home = tmp_path
    (claude_home / ".claude").mkdir()
    result = _run_install(repo_root, claude_home)
    assert result.returncode == 0, result.stderr
    settings = json.loads((claude_home / ".claude" / "settings.json").read_text())
    assert "hooks" in settings
    hook_blocks = settings["hooks"]
    for key in ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd"):
        assert key in hook_blocks
    installed_dir = claude_home / ".claude" / "hooks" / "claude-slack-bot"
    assert (installed_dir / "session_start.sh").is_file()
    assert (installed_dir / "session_start.sh").stat().st_mode & 0o111


def test_install_is_idempotent(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    claude_home = tmp_path
    (claude_home / ".claude").mkdir()
    _run_install(repo_root, claude_home)
    first = (claude_home / ".claude" / "settings.json").read_text()
    _run_install(repo_root, claude_home)
    second = (claude_home / ".claude" / "settings.json").read_text()
    assert first == second
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_install.py -v
```

Expected: FAIL (no install.sh).

- [ ] **Step 3: Write `hooks/install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks/claude-slack-bot"
SETTINGS="${CLAUDE_DIR}/settings.json"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "${HOOKS_DIR}"
for f in session_start.sh prompt.sh stop.sh notify.sh session_end.sh; do
  install -m 0755 "${SOURCE_DIR}/${f}" "${HOOKS_DIR}/${f}"
done

new_hooks="$(jq -n --arg dir "${HOOKS_DIR}" '{
  SessionStart: [{hooks: [{type: "command", command: ($dir + "/session_start.sh")}]}],
  UserPromptSubmit: [{hooks: [{type: "command", command: ($dir + "/prompt.sh")}]}],
  Stop: [{hooks: [{type: "command", command: ($dir + "/stop.sh")}]}],
  Notification: [{hooks: [{type: "command", command: ($dir + "/notify.sh")}]}],
  SessionEnd: [{hooks: [{type: "command", command: ($dir + "/session_end.sh")}]}]
}')"

if [ -f "${SETTINGS}" ]; then
  existing="$(cat "${SETTINGS}")"
else
  existing="{}"
fi

merged="$(jq --argjson new "${new_hooks}" '.hooks = (.hooks // {}) * $new' <<<"${existing}")"

tmp="$(mktemp "${SETTINGS}.XXXXXX")"
printf '%s\n' "${merged}" >"${tmp}"
mv "${tmp}" "${SETTINGS}"

echo "Installed hooks into ${HOOKS_DIR}"
echo "Updated ${SETTINGS}"
```

- [ ] **Step 4: Run shellcheck + tests**

```bash
chmod +x hooks/install.sh
shellcheck hooks/install.sh
uv run pytest tests/test_install.py -v
```

Expected: shellcheck clean; 2 passed.

- [ ] **Step 5: Commit**

```bash
git add hooks/install.sh tests/test_install.py
git commit -m "feat(install): idempotent hook installer with settings.json merge"
```

---

## Task 14: Systemd user unit

**Files:**
- Create: `systemd/claude-slack-bot.service`

- [ ] **Step 1: Write `systemd/claude-slack-bot.service`**

```ini
[Unit]
Description=Bridge Claude Code sessions into Slack
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/claude-slack-bot/env
WorkingDirectory=%h/git/priv/claude-slack-bot
ExecStart=%h/git/priv/claude-slack-bot/.venv/bin/python -m slackbot
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Commit**

```bash
git add systemd/
git commit -m "feat: systemd user unit for the daemon"
```

---

## Task 15: README + manual smoke test

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace `README.md` body**

```markdown
# claude-slack-bot

Bridge between Claude Code sessions and Slack: mirrors selected CC sessions into Slack threads, and types your Slack replies back into the originating Zellij pane.

## What this does

Registers a Claude Code session with `/rn <name>` to opt it into Slack mirroring. The bot posts a top-level message naming the session and mirrors every subsequent prompt, response, and notification into the thread beneath it. Replying in that Slack thread types your reply into the originating Zellij pane (via `zellij action write-chars`).

## Use cases

- Mobile notifications when CC asks for input or finishes a long-running turn
- Reply to CC from your phone — text is typed into the live terminal session
- Monitor multiple parallel CC sessions from one Slack channel
- Long-lived searchable archive of selected bug-bounty sessions

## Requirements

- Linux or WSL2 (with systemd enabled in `/etc/wsl.conf`)
- Python 3.13, `uv`
- Zellij ≥ 0.44
- `jq`, `curl` (for hook scripts)
- A Slack workspace where you can create an app
- Claude Code CLI

## Installation / Setup

### 1. Create the Slack app

1. Visit https://api.slack.com/apps → "Create New App" → "From scratch"
2. Name: `claude-slack-bot`; pick your workspace
3. **Socket Mode** → enable, generate app-level token (`xapp-...`)
4. **OAuth & Permissions** → add bot scopes:
   `chat:write`, `chat:write.public`, `channels:history`, `groups:history`,
   `reactions:write`, `commands`, `app_mentions:read`
5. **Event Subscriptions** → enable, subscribe to bot events: `message.channels`, `message.groups`, `app_mention`
6. Install to workspace; copy `xoxb-...` bot token
7. Invite the bot to the channel you'll use (e.g. `/invite @claude-slack-bot`)
8. Right-click the channel → "Copy link" — the trailing path is the channel ID (`C0123…`)

### 2. Create the env file

```bash
mkdir -p ~/.config/claude-slack-bot
cat > ~/.config/claude-slack-bot/env <<'EOF'
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
SLACK_CHANNEL_ID=C…
SLACKBOT_PORT=8787
LOG_LEVEL=INFO
EOF
chmod 600 ~/.config/claude-slack-bot/env
```

### 3. Install the daemon

```bash
cd ~/git/priv/claude-slack-bot
uv venv && uv sync
mkdir -p ~/.config/systemd/user
cp systemd/claude-slack-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-slack-bot
systemctl --user status claude-slack-bot
```

Expected status: `active (running)`.

### 4. Install the CC hooks

```bash
bash ~/git/priv/claude-slack-bot/hooks/install.sh
```

This copies the five hook scripts into `~/.claude/hooks/claude-slack-bot/` and merges hook entries into `~/.claude/settings.json`.

## Usage

1. Start Claude Code inside a Zellij pane
2. Run `/rn my-session` — top-level message appears in your configured Slack channel
3. Type prompts in CC; they mirror into the Slack thread
4. From Slack (mobile or desktop), reply in the thread — the text is typed into the CC pane (CC pane briefly takes focus)

## Testing

```bash
cd ~/git/priv/claude-slack-bot
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
shellcheck hooks/*.sh
```

Manual smoke test (with daemon running):

```bash
# 1. fake a session_start
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"start","session_id":"smoke","cwd":"/tmp",
       "zellij_session":"main","zellij_pane_id":"0","resumed":false}' \
  http://127.0.0.1:8787/event

# 2. name it (this triggers the Slack post)
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"name","session_id":"smoke","name":"smoke-test"}' \
  http://127.0.0.1:8787/event

# 3. simulate a prompt
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"prompt","session_id":"smoke","text":"hello"}' \
  http://127.0.0.1:8787/event

# 4. end the session
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"end","session_id":"smoke","reason":"done"}' \
  http://127.0.0.1:8787/event
```

Then in Slack: reply to the thread; verify text appears typed into Zellij pane 0 (`zellij --session main attach` to view). Pane briefly steals focus — accepted cost.

## Deployment

`systemd --user` unit (`systemd/claude-slack-bot.service`). Restart-on-failure baked in. Logs:

```bash
journalctl --user -u claude-slack-bot -f
```

## Implemented features

- HTTP event endpoint at `http://127.0.0.1:8787/event`
- SQLite-backed session/event registry
- Slack Socket Mode listener (no inbound port required)
- Top-level message per named session, thread per session lifetime
- Event buffering for prompts that arrive before `/rn`
- Resume detection: same `session_id` → flips top-level back to 🟢
- Name reclaim: new session adopting an existing name reuses the same thread
- Reply routing: thread reply → `zellij action write-chars` into the originating pane
- ✅/⚠️/🚫 emoji reactions confirming delivery state
- Idempotent hook installer

## Planned features

- Multi-session-per-project disambiguation (currently 1:1 enforced)
- `/cc-list`, `/cc-mute`, `/cc-status` slash commands
- Verbose mode posting individual tool calls
- Truncation-with-link for very long responses

## Claude Sessions

| Session | Summary | Date |
|---------|---------|------|
| `slackbot-claude` | Brainstormed, wrote design spec, wrote implementation plan | 2026-05-24 |
```

- [ ] **Step 2: Run full test suite + lint**

```bash
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
shellcheck hooks/*.sh
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, usage, manual smoke test, deployment"
```

---

## Spec Coverage Check

- §3 architecture → Tasks 8, 10, 11
- §4 data model → Tasks 3, 4
- §5 naming/resume → Task 8 (all four resume scenarios covered)
- §6 events posted → Task 5
- §7 Slack side → Tasks 6, 9, 11, 15 (app setup)
- §8 hooks → Tasks 12, 13
- §9 repo layout → Tasks 1, 14
- §10 config → Task 2
- §11 deployment → Tasks 14, 15
- §12 testing → every task ships tests; smoke test in Task 15
- §13 risks → accepted as documented; README note about focus-stealing

After execution: run the manual smoke test (Task 15 step 1), then drive `/rn` from a real CC session to confirm end-to-end behavior.
