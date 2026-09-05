"""SQLite-backed session and event registry. Single-writer, sync."""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  cc_session_id      TEXT PRIMARY KEY,
  agent              TEXT NOT NULL DEFAULT 'claude',
  name               TEXT,
  cwd                TEXT NOT NULL,
  zellij_session     TEXT,
  zellij_pane_id     TEXT,
  matrix_room_id     TEXT,
  matrix_thread_root TEXT,
  cc_pid             INTEGER,
  transcript_path    TEXT,
  pending_notification TEXT,
  pending_question TEXT,
  transcript_offset  INTEGER,
  created_at         INTEGER NOT NULL,
  last_event_at      INTEGER NOT NULL,
  status             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  cc_session_id     TEXT NOT NULL,
  ts                INTEGER NOT NULL,
  kind              TEXT NOT NULL,
  payload           TEXT NOT NULL,
  matrix_event_id   TEXT,
  FOREIGN KEY (cc_session_id) REFERENCES sessions(cc_session_id)
);

CREATE INDEX IF NOT EXISTS idx_event_log_unposted
  ON event_log(cc_session_id) WHERE matrix_event_id IS NULL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Session:
    cc_session_id: str
    agent: str
    name: str | None
    cwd: str
    zellij_session: str | None
    zellij_pane_id: str | None
    matrix_room_id: str | None
    matrix_thread_root: str | None
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
    matrix_event_id: str | None


class Registry:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL gives concurrent readers + a single writer without blocking, and
        # busy_timeout makes transient locks retry instead of failing fast.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._drop_pre_matrix_schema_if_present()
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent column additions for existing DBs.

        executescript(_SCHEMA) uses CREATE TABLE IF NOT EXISTS, so columns
        added to the schema after the table was first created don't appear
        automatically. List each post-initial-schema column here.
        """
        conn = self._c()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "pending_question" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN pending_question TEXT")

    def get_meta(self, key: str) -> str | None:
        """Return a daemon-level scalar, or None if it was never written."""
        row = self._c().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a daemon-level scalar, overwriting any previous value."""
        self._c().execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _drop_pre_matrix_schema_if_present(self) -> None:
        """Detect a pre-Matrix schema (Slack columns) and discard the DB.

        Per the migration plan: this is a single-user deploy, the old DB has
        no value after the cutover because Slack thread IDs are meaningless
        under Matrix. Drop the legacy tables and let the new schema recreate
        from scratch. Idempotent: a fresh DB already has the new schema,
        so the detection short-circuits.
        """
        conn = self._c()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if row is None:
            return
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "slack_channel" in columns or "slack_thread_ts" in columns:
            log.warning("dropping pre-Matrix registry at %s", self._db_path)
            conn.executescript("DROP TABLE IF EXISTS sessions; DROP TABLE IF EXISTS event_log;")

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
        matrix_room_id: str | None = None,
        cc_pid: int | None = None,
        transcript_path: str | None = None,
    ) -> None:
        now = int(time.time())
        self._c().execute(
            """
            INSERT INTO sessions (cc_session_id, agent, cwd, zellij_session, zellij_pane_id,
                                  matrix_room_id, cc_pid, transcript_path,
                                  created_at, last_event_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(cc_session_id) DO UPDATE SET
              agent = excluded.agent,
              cwd = excluded.cwd,
              zellij_session = excluded.zellij_session,
              zellij_pane_id = excluded.zellij_pane_id,
              matrix_room_id = excluded.matrix_room_id,
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
                matrix_room_id,
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
        """Return all sessions that have a Matrix thread bound."""
        rows = (
            self._c()
            .execute("SELECT * FROM sessions WHERE matrix_thread_root IS NOT NULL")
            .fetchall()
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

    def get_session_by_matrix_thread(
        self, thread_root: str, room_id: str | None = None
    ) -> Session | None:
        if room_id:
            row = (
                self._c()
                .execute(
                    "SELECT * FROM sessions WHERE matrix_thread_root = ? "
                    "AND (matrix_room_id = ? OR matrix_room_id IS NULL)",
                    (thread_root, room_id),
                )
                .fetchone()
            )
        else:
            row = (
                self._c()
                .execute(
                    "SELECT * FROM sessions WHERE matrix_thread_root = ?",
                    (thread_root,),
                )
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

    def set_matrix_thread_root(self, cc_session_id: str, thread_root: str) -> None:
        self._c().execute(
            "UPDATE sessions SET matrix_thread_root = ? WHERE cc_session_id = ?",
            (thread_root, cc_session_id),
        )

    def clear_thread_binding(self, cc_session_id: str) -> None:
        """Drop name + thread binding after its thread is deleted, so a later
        event from a still-live session starts a fresh top-level instead of
        posting into the now-redacted thread."""
        self._c().execute(
            "UPDATE sessions SET name = NULL, matrix_thread_root = NULL WHERE cc_session_id = ?",
            (cc_session_id,),
        )

    def repoint_active_sessions(self, room_id: str) -> int:
        """Move every active session into room_id, returning the number moved.

        Sessions pin the room they were created in, so switching to per-host
        routing would otherwise strand pre-existing sessions in whichever room
        they started in. The thread root is dropped along with the room because
        that event lives in the *old* room — keeping it would thread new
        messages onto a root the new room has never seen. The name is kept, so
        each session simply opens a fresh top-level under its own name on its
        next event. Ended sessions are left alone: their threads are history.
        """
        cur = self._c().execute(
            "UPDATE sessions SET matrix_room_id = ?, matrix_thread_root = NULL "
            "WHERE status = 'active' AND matrix_room_id != ?",
            (room_id, room_id),
        )
        return cur.rowcount

    def set_pending_notification(
        self,
        cc_session_id: str,
        ts: str,
        text: str,
        room_id: str | None,
    ) -> None:
        """Persist the most recently posted notification so the resolved-marker
        edit survives worker reap and daemon restart."""
        import json as _json

        payload = _json.dumps({"ts": ts, "text": text, "room_id": room_id or ""})
        self._c().execute(
            "UPDATE sessions SET pending_notification = ? WHERE cc_session_id = ?",
            (payload, cc_session_id),
        )

    def consume_pending_notification(self, cc_session_id: str) -> dict[str, str] | None:
        """Atomically read + clear the pending notification. Returns
        {ts, text, room_id} or None."""
        import json as _json

        conn = self._c()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT pending_notification FROM sessions WHERE cc_session_id = ?",
                (cc_session_id,),
            ).fetchone()
            if row is None or row["pending_notification"] is None:
                conn.execute("COMMIT")
                return None
            payload = row["pending_notification"]
            conn.execute(
                "UPDATE sessions SET pending_notification = NULL WHERE cc_session_id = ?",
                (cc_session_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        try:
            return _json.loads(payload)
        except (ValueError, TypeError):
            return None

    def set_pending_question(self, cc_session_id: str, options: list[dict]) -> None:
        """Persist that CC is mid-AskUserQuestion. Reply routing reads this
        so a numeric reply maps to the matching option (via arrow-key
        navigation in the actuator) rather than typed verbatim into the
        UI's "Other" text field."""
        import json as _json

        payload = _json.dumps({"options": options})
        self._c().execute(
            "UPDATE sessions SET pending_question = ? WHERE cc_session_id = ?",
            (payload, cc_session_id),
        )

    def get_pending_question(self, cc_session_id: str) -> dict | None:
        """Read pending_question WITHOUT clearing. Routing uses this so the
        state survives across multiple reply attempts; explicit
        clear_pending_question() fires after a successful delivery or when
        CC's next response shows the question has advanced."""
        import json as _json

        row = (
            self._c()
            .execute(
                "SELECT pending_question FROM sessions WHERE cc_session_id = ?",
                (cc_session_id,),
            )
            .fetchone()
        )
        if row is None or row["pending_question"] is None:
            return None
        try:
            return _json.loads(row["pending_question"])
        except (ValueError, TypeError):
            return None

    def clear_pending_question(self, cc_session_id: str) -> None:
        self._c().execute(
            "UPDATE sessions SET pending_question = NULL WHERE cc_session_id = ?",
            (cc_session_id,),
        )

    def set_status(self, cc_session_id: str, status: str) -> None:
        self._c().execute(
            "UPDATE sessions SET status = ?, last_event_at = ? WHERE cc_session_id = ?",
            (status, int(time.time()), cc_session_id),
        )

    def find_recoverable_session(
        self,
        zellij_session: str | None,
        zellij_pane_id: str | None,
        cwd: str,
        agent: str,
        exclude_sid: str,
    ) -> Session | None:
        """Find any named predecessor in the EXACT SAME zellij pane.

        Matching on cwd + zellij_session alone is too loose: a single
        project dir typically holds several CCs across panes, and the
        first-named one would silently steal every other restart's
        identity. Pane id is the only field that's truly per-CC.

        The caller decides whether the candidate is actually dead via
        session_is_alive.
        """
        if not zellij_pane_id:
            return None
        row = (
            self._c()
            .execute(
                """
                SELECT * FROM sessions
                WHERE name IS NOT NULL
                  AND cwd = ?
                  AND zellij_session IS ?
                  AND zellij_pane_id = ?
                  AND agent = ?
                  AND cc_session_id != ?
                ORDER BY last_event_at DESC
                LIMIT 1
                """,
                (cwd, zellij_session, zellij_pane_id, agent, exclude_sid),
            )
            .fetchone()
        )
        return _row_to_session(row) if row else None

    def get_session_by_name(self, name: str, room_id: str | None = None) -> Session | None:
        """Return the most recently active session bound to *name*, or None.

        Used to enforce uniqueness for the Matrix-side `/new <name>` command.
        Restricting to a room prevents collisions across per-agent rooms.
        """
        if room_id is None:
            row = (
                self._c()
                .execute(
                    "SELECT * FROM sessions WHERE name = ? ORDER BY last_event_at DESC LIMIT 1",
                    (name,),
                )
                .fetchone()
            )
        else:
            row = (
                self._c()
                .execute(
                    "SELECT * FROM sessions WHERE name = ? AND matrix_room_id = ? "
                    "ORDER BY last_event_at DESC LIMIT 1",
                    (name, room_id),
                )
                .fetchone()
            )
        return _row_to_session(row) if row else None

    def reserve_name(self, name: str, room_id: str, thread_root: str) -> str:
        """Insert a placeholder session row that owns *name* until a real CC binds.

        Returns the synthetic cc_session_id of the placeholder. When a real
        session later calls /rn with this name, `claim_name` transfers the
        thread off the placeholder (which is then a harmless dust row).
        """
        synthetic_sid = f"reserved:{uuid.uuid4()}"
        now = int(time.time())
        self._c().execute(
            "INSERT INTO sessions(cc_session_id, agent, name, cwd, "
            "matrix_room_id, matrix_thread_root, created_at, last_event_at, status) "
            "VALUES (?, 'claude', ?, '(reserved)', ?, ?, ?, ?, 'reserved')",
            (synthetic_sid, name, room_id, thread_root, now, now),
        )
        return synthetic_sid

    def claim_name(self, cc_session_id: str, name: str) -> str | None:
        """Claim `name` for `cc_session_id`. Returns prior holder's thread_root
        (or None).

        Wrapped in BEGIN IMMEDIATE/COMMIT so a concurrent claim cannot leave
        two rows owning the same name.
        """
        conn = self._c()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT matrix_room_id FROM sessions WHERE cc_session_id = ?",
                (cc_session_id,),
            ).fetchone()
            current_room = current["matrix_room_id"] if current else None
            prior = conn.execute(
                "SELECT cc_session_id, matrix_room_id, matrix_thread_root FROM sessions "
                "WHERE name = ? AND cc_session_id != ?",
                (name, cc_session_id),
            ).fetchone()
            prior_thread: str | None = None
            if prior and prior["matrix_room_id"] == current_room:
                prior_thread = prior["matrix_thread_root"]
            if prior:
                conn.execute(
                    "UPDATE sessions SET name = NULL, matrix_thread_root = NULL "
                    "WHERE cc_session_id = ?",
                    (prior["cc_session_id"],),
                )
            conn.execute(
                "UPDATE sessions SET name = ?, last_event_at = ? WHERE cc_session_id = ?",
                (name, int(time.time()), cc_session_id),
            )
            if prior_thread:
                conn.execute(
                    "UPDATE sessions SET matrix_thread_root = ? WHERE cc_session_id = ?",
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
                "SELECT id, cc_session_id, ts, kind, payload, matrix_event_id FROM event_log "
                "WHERE cc_session_id = ? AND matrix_event_id IS NULL ORDER BY id ASC",
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
                matrix_event_id=r["matrix_event_id"],
            )
            for r in rows
        ]

    def mark_event_posted(self, event_id: int, matrix_event_id: str) -> None:
        self._c().execute(
            "UPDATE event_log SET matrix_event_id = ? WHERE id = ?",
            (matrix_event_id, event_id),
        )

    def set_transcript_offset(self, cc_session_id: str, offset: int) -> None:
        """Persist the current transcript byte offset so a daemon restart can
        resume reading from the same point instead of snapping to EOF."""
        self._c().execute(
            "UPDATE sessions SET transcript_offset = ? WHERE cc_session_id = ?",
            (offset, cc_session_id),
        )

    def set_transcript_path(self, cc_session_id: str, transcript_path: str) -> None:
        """Record where this session's transcript lives.

        Only fills a NULL: an established path is the one the reader is
        already tailing, and overwriting it would silently move the cursor.
        """
        self._c().execute(
            "UPDATE sessions SET transcript_path = ? "
            "WHERE cc_session_id = ? AND transcript_path IS NULL",
            (transcript_path, cc_session_id),
        )

    def get_transcript_offset(self, cc_session_id: str) -> int | None:
        row = (
            self._c()
            .execute(
                "SELECT transcript_offset FROM sessions WHERE cc_session_id = ?",
                (cc_session_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        value = row["transcript_offset"]
        return int(value) if value is not None else None

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
        matrix_room_id=row["matrix_room_id"],
        matrix_thread_root=row["matrix_thread_root"],
        cc_pid=row["cc_pid"],
        transcript_path=row["transcript_path"],
        created_at=row["created_at"],
        last_event_at=row["last_event_at"],
        status=row["status"],
    )
