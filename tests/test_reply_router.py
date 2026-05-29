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
    await router.on_reply(channel="C-CLAUDE", thread_ts="TOP.1", text="do X", msg_ts="MSG.1")
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
    # Clock advanced well past the recent-hook window so the liveness check actually runs.
    future_clock = lambda: 1e12  # noqa: E731
    router = ReplyRouter(
        reg=reg, supervisor=sup, liveness=_alive_cache(False), slack=slack, clock=future_clock
    )
    await router.on_reply(channel="C-CLAUDE", thread_ts="TOP.1", text="x", msg_ts="MSG.1")
    assert any("No running CC process" in t for _, t in slack.thread_posts)
    assert ("MSG.1", "no_entry_sign") in slack.reacts
    sess = reg.get_session("s1")
    assert sess is not None and sess.status == "ended"
    await sup.shutdown()


@pytest.mark.asyncio
async def test_recent_hook_activity_bypasses_strict_liveness_check(reg: Registry) -> None:
    """A CC whose argv doesn't include session_id or --resume <name> would fail
    the strict process scan. Recent hook activity (any /event POST in the last
    2 min) is independent proof of life: deliver the reply anyway."""
    reg.upsert_session("s1", "/x", "main", "3", agent="claude", slack_channel="C-CLAUDE")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    # Clock is now()-ish so last_event_at (set by upsert) is recent.
    router = ReplyRouter(reg=reg, supervisor=sup, liveness=_alive_cache(False), slack=slack)
    await router.on_reply(channel="C-CLAUDE", thread_ts="TOP.1", text="do X", msg_ts="MSG.1")
    # No rejection, reply enqueued into worker.
    assert slack.thread_posts == []
    assert slack.reacts == []
    worker = sup._workers["s1"]
    assert worker._queue.qsize() == 1
    await sup.shutdown()


@pytest.mark.asyncio
async def test_reply_for_unknown_thread_ignored(reg: Registry) -> None:
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    router = ReplyRouter(reg=reg, supervisor=sup, liveness=_alive_cache(True), slack=slack)
    await router.on_reply(channel="C-CLAUDE", thread_ts="UNKNOWN", text="x", msg_ts="MSG.1")
    assert slack.thread_posts == []
    assert slack.reacts == []
    await sup.shutdown()
