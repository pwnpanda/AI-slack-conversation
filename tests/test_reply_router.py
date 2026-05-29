from dataclasses import dataclass, field

import pytest

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


@pytest.mark.asyncio
async def test_reply_enqueues_into_worker(reg: Registry) -> None:
    reg.upsert_session("s1", "/x", "main", "3", agent="claude", slack_channel="C-CLAUDE")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    router = ReplyRouter(reg=reg, supervisor=sup, slack=slack)
    await router.on_reply(channel="C-CLAUDE", thread_ts="TOP.1", text="do X", msg_ts="MSG.1")
    worker = sup._workers["s1"]
    assert worker._queue.qsize() == 1
    # No upfront rejection: the actuator is the ground truth for delivery
    # failure; the worker handles that path and reacts ⚠️ on failure.
    assert slack.thread_posts == []
    assert slack.reacts == []
    await sup.shutdown()


@pytest.mark.asyncio
async def test_reply_for_unknown_thread_ignored(reg: Registry) -> None:
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    router = ReplyRouter(reg=reg, supervisor=sup, slack=slack)
    await router.on_reply(channel="C-CLAUDE", thread_ts="UNKNOWN", text="x", msg_ts="MSG.1")
    assert slack.thread_posts == []
    assert slack.reacts == []
    await sup.shutdown()


@pytest.mark.asyncio
async def test_idle_session_reply_is_not_rejected_upfront(reg: Registry) -> None:
    """An idle CC session (no hook events for ages) used to be falsely
    rejected by the old upfront liveness check. Now the worker + actuator
    decide; if the pane still works, the reply is delivered."""
    reg.upsert_session("s1", "/x", "main", "3", agent="claude", slack_channel="C-CLAUDE")
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "TOP.1")
    # Age last_event_at well past any historical "recent activity" window.
    reg._c().execute("UPDATE sessions SET last_event_at = 0 WHERE cc_session_id = 's1'")
    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=_NoopActuator())
    router = ReplyRouter(reg=reg, supervisor=sup, slack=slack)
    await router.on_reply(channel="C-CLAUDE", thread_ts="TOP.1", text="hi", msg_ts="MSG.1")
    assert slack.thread_posts == []
    assert slack.reacts == []
    worker = sup._workers["s1"]
    assert worker._queue.qsize() == 1
    await sup.shutdown()
