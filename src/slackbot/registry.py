"""SQLite-backed session and event registry. Single-writer, sync."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class Session:
    cc_session_id: str
    agent: str
    name: str | None
    cwd: str
    zellij_session: str | None
    zellij_pane_id: str | None
    slack_channel: str | None
    slack_thread_ts: str | None
    cc_pid: int | None
    transcript_path: str | None
    created_at: int
    last_event_at: int
    status: str


@dataclass(frozen=True)
class Event:
    id: int
    cc_session_id: str
    ts: int
    kind: str
    payload: str
    slack_msg_ts: str | None


class Registry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {
            row["name"] for row in self._c().execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "agent" not in columns:
            self._c().execute(
                "ALTER TABLE sessions ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'"
            )
        if "cc_pid" not in columns:
            self._c().execute("ALTER TABLE sessions ADD COLUMN cc_pid INTEGER")
        if "transcript_path" not in columns:
            self._c().execute("ALTER TABLE sessions ADD COLUMN transcript_path TEXT")

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

    def get_session(self, cc_session_id: str) -> Session | None:
        row = (
            self._c()
            .execute("SELECT * FROM sessions WHERE cc_session_id = ?", (cc_session_id,))
            .fetchone()
        )
        return _row_to_session(row) if row else None

    def list_threads(self) -> list[Session]:
        """Return all sessions that have a Slack thread bound — used by the
        web-API fallback poller to know which threads to poll."""
        rows = (
            self._c().execute("SELECT * FROM sessions WHERE slack_thread_ts IS NOT NULL").fetchall()
        )
        return [_row_to_session(r) for r in rows]

    def list_active_with_transcript(self) -> list[Session]:
        """Return active sessions whose transcript_path is recorded.

        Used at daemon startup to re-attach transcript readers for CCs that
        were already running when the daemon last shut down.
        """
        rows = (
            self._c()
            .execute(
                "SELECT * FROM sessions "
                "WHERE status='active' AND transcript_path IS NOT NULL "
                "ORDER BY last_event_at DESC"
            )
            .fetchall()
        )
        return [_row_to_session(r) for r in rows]

    def get_session_by_thread(self, thread_ts: str, channel: str | None = None) -> Session | None:
        if channel:
            row = (
                self._c()
                .execute(
                    "SELECT * FROM sessions "
                    "WHERE slack_thread_ts = ? AND (slack_channel = ? OR slack_channel IS NULL)",
                    (thread_ts, channel),
                )
                .fetchone()
            )
        else:
            row = (
                self._c()
                .execute("SELECT * FROM sessions WHERE slack_thread_ts = ?", (thread_ts,))
                .fetchone()
            )
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

    def find_recoverable_session(
        self,
        zellij_session: str | None,
        cwd: str,
        agent: str,
        exclude_sid: str,
    ) -> Session | None:
        """Find any named predecessor in the same workspace. The caller decides
        whether the candidate is actually dead via session_is_alive."""
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

    def buffer_event(self, cc_session_id: str, kind: str, payload: str) -> int:
        cur = self._c().execute(
            "INSERT INTO event_log (cc_session_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (cc_session_id, int(time.time()), kind, payload),
        )
        return cur.lastrowid or 0

    def drain_unposted(self, cc_session_id: str) -> list[Event]:
        rows = (
            self._c()
            .execute(
                "SELECT id, cc_session_id, ts, kind, payload, slack_msg_ts FROM event_log "
                "WHERE cc_session_id = ? AND slack_msg_ts IS NULL ORDER BY id ASC",
                (cc_session_id,),
            )
            .fetchall()
        )
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


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        cc_session_id=row["cc_session_id"],
        agent=row["agent"],
        name=row["name"],
        cwd=row["cwd"],
        zellij_session=row["zellij_session"],
        zellij_pane_id=row["zellij_pane_id"],
        slack_channel=row["slack_channel"],
        slack_thread_ts=row["slack_thread_ts"],
        cc_pid=row["cc_pid"],
        transcript_path=row["transcript_path"],
        created_at=row["created_at"],
        last_event_at=row["last_event_at"],
        status=row["status"],
    )
