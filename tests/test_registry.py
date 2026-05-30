import sqlite3
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
        agent="codex",
        matrix_room_id="!codex:matrix.example.com",
    )
    sess = reg.get_session("abc")
    assert sess is not None
    assert sess.cc_session_id == "abc"
    assert sess.cwd == "/home/r/p"
    assert sess.zellij_session == "default"
    assert sess.zellij_pane_id == "0"
    assert sess.agent == "codex"
    assert sess.matrix_room_id == "!codex:matrix.example.com"
    assert sess.name is None
    assert sess.status == "active"
    assert sess.matrix_thread_root is None
    reg.close()


def test_upsert_session_updates_zellij_on_resume(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("abc", "/x", "s1", "p1")
    reg.set_name("abc", "myname")
    reg.set_matrix_thread_root("abc", "$root:server")
    reg.set_status("abc", "ended")
    reg.upsert_session("abc", "/x", "s2", "p2")  # resume
    sess = reg.get_session("abc")
    assert sess is not None
    assert sess.name == "myname"
    assert sess.matrix_thread_root == "$root:server"
    assert sess.zellij_session == "s2"
    assert sess.zellij_pane_id == "p2"
    assert sess.status == "active"
    reg.close()


def test_claim_name_returns_prior_holder(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("old", "/x", "s", "p1")
    reg.set_name("old", "shared")
    reg.set_matrix_thread_root("old", "$prior:server")
    reg.upsert_session("new", "/x", "s", "p2")
    prior_thread = reg.claim_name("new", "shared")
    assert prior_thread == "$prior:server"
    old_sess = reg.get_session("old")
    assert old_sess is not None and old_sess.name is None
    assert old_sess.matrix_thread_root is None  # ownership transferred away
    new_sess = reg.get_session("new")
    assert new_sess is not None
    assert new_sess.name == "shared"
    assert new_sess.matrix_thread_root == "$prior:server"
    # thread lookup now resolves to the new claimant
    thread_sess = reg.get_session_by_matrix_thread("$prior:server")
    assert thread_sess is not None
    assert thread_sess.cc_session_id == "new"
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


def test_buffer_and_drain_events(tmp_db_path: str) -> None:
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
        reg.mark_event_posted(ev.id, "$evt:server")
    assert reg.drain_unposted("abc") == []
    reg.close()


def test_buffer_event_preserves_order(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("abc", "/x", "s", "p")
    for i in range(5):
        reg.buffer_event("abc", "prompt", f'{{"i":{i}}}')
    events = reg.drain_unposted("abc")
    assert [e.payload for e in events] == [f'{{"i":{i}}}' for i in range(5)]
    reg.close()


def test_refresh_liveness_updates_fields_without_status_change(tmp_db_path: str) -> None:
    """refresh_liveness updates pid/pane/timestamp but leaves status untouched."""
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "3", cc_pid=100)
    reg.set_status("s1", "ended")
    assert reg.get_session("s1").status == "ended"
    reg.refresh_liveness("s1", "main", "5", 200)
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.status == "ended"  # NOT flipped back to active
    assert sess.cc_pid == 200
    assert sess.zellij_pane_id == "5"
    reg.close()


def test_upsert_session_persists_transcript_path(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "3", transcript_path="/tmp/tx.jsonl")
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.transcript_path == "/tmp/tx.jsonl"
    reg.close()


def test_refresh_liveness_does_not_flip_status(tmp_db_path: str) -> None:
    """status is diagnostic-only now; refresh must not touch it."""
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


def test_transcript_offset_round_trip(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "3", transcript_path="/tmp/tx.jsonl")
    # Initially unset.
    assert reg.get_transcript_offset("s1") is None
    reg.set_transcript_offset("s1", 1234)
    assert reg.get_transcript_offset("s1") == 1234
    reg.set_transcript_offset("s1", 5678)
    assert reg.get_transcript_offset("s1") == 5678
    reg.close()


def test_open_sets_wal_and_busy_timeout(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    conn = reg._c()
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000
    reg.close()


def test_claim_name_is_atomic(tmp_db_path: str) -> None:
    """Two concurrent claims of the same name end with exactly one row owning it."""
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("a", "/x", "main", "1")
    reg.upsert_session("b", "/x", "main", "2")
    reg.claim_name("a", "shared")
    reg.set_matrix_thread_root("a", "$T1:server")
    prior = reg.claim_name("b", "shared")
    assert prior == "$T1:server"
    a = reg.get_session("a")
    b = reg.get_session("b")
    assert a is not None and a.name is None and a.matrix_thread_root is None
    assert b is not None and b.name == "shared" and b.matrix_thread_root == "$T1:server"
    reg.close()


def test_reserve_name_creates_placeholder_and_lookup_finds_it(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    sid = reg.reserve_name("kbd", "!claude:server", "$TOP42:server")
    assert sid.startswith("reserved:")
    sess = reg.get_session_by_name("kbd", room_id="!claude:server")
    assert sess is not None
    assert sess.name == "kbd"
    assert sess.matrix_thread_root == "$TOP42:server"
    assert sess.status == "reserved"
    assert sess.cc_session_id == sid
    reg.close()


def test_get_session_by_name_scoped_by_room(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.reserve_name("kbd", "!a:server", "$T1:server")
    assert reg.get_session_by_name("kbd", room_id="!a:server") is not None
    assert reg.get_session_by_name("kbd", room_id="!b:server") is None
    assert reg.get_session_by_name("kbd") is not None
    reg.close()


def test_real_session_claiming_reserved_name_inherits_thread(tmp_db_path: str) -> None:
    """The reserved row holds the thread until a real CC binds via /rn."""
    reg = Registry(tmp_db_path)
    reg.open()
    reg.reserve_name("kbd", "!claude:server", "$TOP42:server")
    reg.upsert_session("real-sid", "/x", "main", "1", matrix_room_id="!claude:server")
    prior = reg.claim_name("real-sid", "kbd")
    assert prior == "$TOP42:server"
    real = reg.get_session("real-sid")
    assert real is not None and real.matrix_thread_root == "$TOP42:server" and real.name == "kbd"
    reg.close()


def test_open_drops_pre_matrix_schema(tmp_db_path: str) -> None:
    """A legacy DB with Slack columns is discarded on open and recreated."""
    # Build a pre-Matrix schema manually.
    conn = sqlite3.connect(tmp_db_path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE sessions (
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
          pending_notification TEXT,
          created_at      INTEGER NOT NULL,
          last_event_at   INTEGER NOT NULL,
          status          TEXT NOT NULL
        );
        CREATE TABLE event_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cc_session_id TEXT,
          ts INTEGER,
          kind TEXT,
          payload TEXT,
          slack_msg_ts TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions(cc_session_id, agent, cwd, created_at, last_event_at, "
        "status, slack_channel, slack_thread_ts) "
        "VALUES('old', 'claude', '/x', 1, 1, 'active', 'C-OLD', 'T.OLD')"
    )
    conn.close()

    reg = Registry(tmp_db_path)
    reg.open()
    # Old row must be gone; new schema must be present.
    assert reg.get_session("old") is None
    cols = {r["name"] for r in reg._c().execute("PRAGMA table_info(sessions)").fetchall()}
    assert "matrix_room_id" in cols
    assert "matrix_thread_root" in cols
    assert "slack_channel" not in cols
    reg.close()
