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
        slack_channel="C-CODEX",
    )
    sess = reg.get_session("abc")
    assert sess is not None
    assert sess.cc_session_id == "abc"
    assert sess.cwd == "/home/r/p"
    assert sess.zellij_session == "default"
    assert sess.zellij_pane_id == "0"
    assert sess.agent == "codex"
    assert sess.slack_channel == "C-CODEX"
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
    assert old_sess.slack_thread_ts is None  # ownership transferred away
    new_sess = reg.get_session("new")
    assert new_sess is not None
    assert new_sess.name == "shared"
    assert new_sess.slack_thread_ts == "111.222"
    # thread lookup now resolves to the new claimant
    thread_sess = reg.get_session_by_thread("111.222")
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
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("a", "/x", "main", "1")
    reg.upsert_session("b", "/x", "main", "2")
    reg.claim_name("a", "shared")
    reg.set_thread_ts("a", "T.1")
    prior = reg.claim_name("b", "shared")
    assert prior == "T.1"
    a = reg.get_session("a")
    b = reg.get_session("b")
    assert a is not None and a.name is None and a.slack_thread_ts is None
    assert b is not None and b.name == "shared" and b.slack_thread_ts == "T.1"
    reg.close()
