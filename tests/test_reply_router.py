import time
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
    thread_channels: list[str | None] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    react_channels: list[str | None] = field(default_factory=list)

    async def post_in_thread(self, thread_ts: str, text: str, channel: str | None = None) -> str:
        self.thread_posts.append((thread_ts, text))
        self.thread_channels.append(channel)
        return "x.x"

    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None:
        self.reacts.append((ts, emoji))
        self.react_channels.append(channel)


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


@pytest.mark.asyncio
async def test_reply_routes_to_correct_pane(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3", agent="codex", slack_channel="C-CODEX")
    reg.set_name("s1", "myproj")
    reg.set_thread_ts("s1", "TOP.1")
    actuator = FakeActuator()
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack)
    await router.on_reply(channel="C-CODEX", thread_ts="TOP.1", text="do X", msg_ts="MSG.1")
    assert actuator.deliveries == [("main", "3", "do X")]
    assert ("MSG.1", "white_check_mark") in slack.reacts
    assert "C-CODEX" in slack.react_channels


@pytest.mark.asyncio
async def test_reply_for_unknown_thread_ignored(reg: Registry) -> None:
    actuator = FakeActuator()
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack)
    await router.on_reply(channel="C1", thread_ts="UNKNOWN", text="x", msg_ts="MSG.1")
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
    await router.on_reply(channel="C1", thread_ts="TOP.1", text="x", msg_ts="MSG.1")
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
    await router.on_reply(channel="C1", thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert any("delivery failed" in t for _, t in slack.thread_posts)
    assert ("MSG.1", "warning") in slack.reacts


@pytest.mark.asyncio
async def test_reply_to_dead_cc_pid_warns_and_marks_ended(reg: Registry) -> None:
    """When the recorded CC PID is no longer running, refuse delivery."""
    reg.upsert_session("s1", "/x", "main", "3", cc_pid=99999)
    reg.set_name("s1", "Finance")
    reg.set_thread_ts("s1", "TOP.1")
    actuator = FakeActuator()
    slack = FakeSlackIO()

    # Inject a pid_alive_fn that always returns False to simulate dead CC.
    router = ReplyRouter(reg, actuator, slack, pid_alive_fn=lambda _: False)
    await router.on_reply(channel="C1", thread_ts="TOP.1", text="x", msg_ts="MSG.1")

    assert actuator.deliveries == []
    assert any("no longer running" in t for _, t in slack.thread_posts)
    assert any("auto-rebind" in t for _, t in slack.thread_posts)
    assert ("MSG.1", "no_entry_sign") in slack.reacts
    sess = reg.get_session("s1")
    assert sess is not None and sess.status == "ended"


@pytest.mark.asyncio
async def test_reply_to_idle_but_alive_session_still_delivers(reg: Registry) -> None:
    """An alive CC that's been idle for hours/days must STILL receive replies."""
    reg.upsert_session("s1", "/x", "main", "3", cc_pid=12345)
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    # Backdate last_event_at to 48h ago. No time-based stale anymore — only
    # process liveness matters.
    reg._c().execute(
        "UPDATE sessions SET last_event_at = ? WHERE cc_session_id = ?",
        (int(time.time()) - 48 * 3600, "s1"),
    )
    actuator = FakeActuator()
    slack = FakeSlackIO()
    router = ReplyRouter(reg, actuator, slack, pid_alive_fn=lambda _: True)
    await router.on_reply(channel="C1", thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert actuator.deliveries == [("main", "3", "x")]


@pytest.mark.asyncio
async def test_reply_to_legacy_row_without_pid_still_delivers(reg: Registry) -> None:
    """Rows that predate cc_pid have NULL pid — must not be treated as dead."""
    reg.upsert_session("s1", "/x", "main", "3")  # no cc_pid
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    actuator = FakeActuator()
    slack = FakeSlackIO()
    # Even if pid_alive returned False, sess.cc_pid is None so we shouldn't call it.
    sentinel_called = []

    def boom(_pid):
        sentinel_called.append(True)
        return False

    router = ReplyRouter(reg, actuator, slack, pid_alive_fn=boom)
    await router.on_reply(channel="C1", thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert actuator.deliveries == [("main", "3", "x")]
    assert sentinel_called == []  # short-circuit when cc_pid is None
