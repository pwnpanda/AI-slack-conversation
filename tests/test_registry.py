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
