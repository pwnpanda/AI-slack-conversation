from dataclasses import dataclass, field

import pytest

from slackbot.registry import Registry
from slackbot.slack_poller import _RESTART_REPLAY_SECONDS, SlackPoller


@dataclass
class FakeWebClient:
    """Returns canned conversations.replies responses keyed by thread_ts."""

    by_thread: dict[str, list[dict]] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def conversations_replies(self, **kwargs):
        channel = kwargs.get("channel")
        ts = kwargs.get("ts")
        oldest = kwargs.get("oldest")
        self.calls.append((channel, ts, oldest))
        return {"ok": True, "messages": self.by_thread.get(ts, [])}


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


@pytest.mark.asyncio
async def test_poller_baseline_skips_existing(reg: Registry) -> None:
    """Baseline initialization should not replay history."""
    reg.upsert_session("s1", "/x", "main", "3", slack_channel="C1")
    reg.set_thread_ts("s1", "T.1")
    web = FakeWebClient(by_thread={"T.1": [{"ts": "1.0", "text": "old"}]})
    delivered: list = []

    async def deliver(channel, thread_ts, text, msg_ts):
        delivered.append((channel, thread_ts, text, msg_ts))

    poller = SlackPoller(reg, web, deliver, interval_seconds=99)
    poller.baseline_now()
    await poller._poll_once()
    assert delivered == []  # historical message older than baseline


@pytest.mark.asyncio
async def test_poller_delivers_new_messages(reg: Registry) -> None:
    """Messages with ts > baseline get delivered."""
    import time as t

    reg.upsert_session("s1", "/x", "main", "3", slack_channel="C1")
    reg.set_thread_ts("s1", "T.1")

    delivered: list = []

    async def deliver(channel, thread_ts, text, msg_ts):
        delivered.append((channel, thread_ts, text, msg_ts))

    web = FakeWebClient()
    poller = SlackPoller(reg, web, deliver, interval_seconds=99)
    poller.baseline_now()
    # Slack adds a message AFTER baseline
    new_ts = t.time() + 1.0
    web.by_thread["T.1"] = [{"ts": f"{new_ts:.6f}", "text": "hi"}]
    await poller._poll_once()
    assert len(delivered) == 1
    assert delivered[0][2] == "hi"


@pytest.mark.asyncio
async def test_poller_skips_bot_and_subtype_messages(reg: Registry) -> None:
    import time as t

    reg.upsert_session("s1", "/x", "main", "3", slack_channel="C1")
    reg.set_thread_ts("s1", "T.1")
    delivered: list = []

    async def deliver(channel, thread_ts, text, msg_ts):
        delivered.append((channel, thread_ts, text, msg_ts))

    web = FakeWebClient()
    poller = SlackPoller(reg, web, deliver, interval_seconds=99)
    poller.baseline_now()
    new_ts = t.time() + 1.0
    web.by_thread["T.1"] = [
        {"ts": f"{new_ts:.6f}", "text": "bot", "bot_id": "B1"},
        {"ts": f"{new_ts + 0.1:.6f}", "text": "edit", "subtype": "message_changed"},
        {"ts": f"{new_ts + 0.2:.6f}", "text": "real"},
    ]
    await poller._poll_once()
    assert len(delivered) == 1
    assert delivered[0][2] == "real"


def test_baseline_uses_restart_replay_window(reg: Registry) -> None:
    """Baseline must be `now - _RESTART_REPLAY_SECONDS`, not `now`, so replies
    delivered while the daemon was down still get picked up after restart."""
    import time as t

    reg.upsert_session("s1", "/x", "main", "3", slack_channel="C1")
    reg.set_thread_ts("s1", "T.1")
    poller = SlackPoller(reg, FakeWebClient(), lambda *a, **kw: None, interval_seconds=99)
    before = t.time()
    poller.baseline_now()
    after = t.time()
    cursor = poller._seen_after["T.1"]
    # Cursor sits in the window [before - replay, after - replay].
    assert before - _RESTART_REPLAY_SECONDS <= cursor <= after - _RESTART_REPLAY_SECONDS
    # And it is meaningfully earlier than "now" — at least replay - 1s.
    assert (after - cursor) >= _RESTART_REPLAY_SECONDS - 1.0


@pytest.mark.asyncio
async def test_poller_does_not_redeliver(reg: Registry) -> None:
    """A second poll with no new messages must not re-deliver the previous batch."""
    import time as t

    reg.upsert_session("s1", "/x", "main", "3", slack_channel="C1")
    reg.set_thread_ts("s1", "T.1")
    delivered: list = []

    async def deliver(channel, thread_ts, text, msg_ts):
        delivered.append(msg_ts)

    web = FakeWebClient()
    poller = SlackPoller(reg, web, deliver, interval_seconds=99)
    poller.baseline_now()
    new_ts = t.time() + 1.0
    web.by_thread["T.1"] = [{"ts": f"{new_ts:.6f}", "text": "hi"}]
    await poller._poll_once()
    await poller._poll_once()
    assert len(delivered) == 1
