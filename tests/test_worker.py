from dataclasses import dataclass, field

import pytest

from slackbot.worker import Worker


@dataclass
class FakeSlackIO:
    posts: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    top_level_posts: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    _ts: int = 0

    def channel_for_agent(self, agent: str) -> str:
        return f"C-{agent.upper()}"

    async def post_in_thread(self, thread_ts, text, channel=None):
        self.posts.append((thread_ts, text))
        self._ts += 1
        return f"thr.{self._ts}"

    async def post_top_level(self, text, channel=None):
        self.top_level_posts.append(text)
        self._ts += 1
        return f"top.{self._ts}"

    async def edit_top_level(self, ts, text, channel=None):
        self.edits.append((ts, text))

    async def react(self, ts, emoji, channel=None):
        self.reacts.append((ts, emoji))


@dataclass
class FakeActuator:
    deliveries: list[tuple[str, str, str]] = field(default_factory=list)

    async def deliver(self, session, pane_id, text):
        self.deliveries.append((session, pane_id, text))


def _bound_session(reg, sid, agent="claude"):
    """Helper: create a registered+named session with a thread."""
    reg.upsert_session(sid, "/x", "main", "13", agent=agent, slack_channel="C-CLAUDE")
    reg.set_name(sid, "myproj")
    reg.set_thread_ts(sid, "TOP.1")


@pytest.mark.asyncio
async def test_worker_mirrors_assistant_response(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hello"})
    await worker.stop()

    assert slack.posts == [("TOP.1", "[Claude] 🤖 hello")]
    reg.close()


@pytest.mark.asyncio
async def test_worker_dedupes_by_uuid(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    ev = {"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hello"}
    await worker.enqueue(ev)
    await worker.enqueue(ev)
    await worker.stop()

    assert len(slack.posts) == 1
    reg.close()


@pytest.mark.asyncio
async def test_worker_delivers_slack_reply_to_pane(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    actuator = FakeActuator()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=actuator)
    await worker.start()

    await worker.enqueue({"kind": "slack_reply", "text": "do X", "msg_ts": "MSG.1"})
    await worker.stop()

    assert actuator.deliveries == [("main", "13", "do X")]
    assert ("MSG.1", "white_check_mark") in slack.reacts
    reg.close()


@pytest.mark.asyncio
async def test_worker_suppresses_echoed_prompt_after_delivery(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    # Slack reply is delivered to pane → echo expected.
    await worker.enqueue({"kind": "slack_reply", "text": "ping", "msg_ts": "MSG.1"})
    # Transcript reader will see the user typing it and emit prompt with same text.
    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "ping"})
    await worker.stop()

    # The slack reply's reaction was added, but the prompt was suppressed (not mirrored).
    assert ("MSG.1", "white_check_mark") in slack.reacts
    assert slack.posts == []  # no 👤 ping mirrored
    reg.close()


@pytest.mark.asyncio
async def test_worker_mirrors_organic_prompt(tmp_db_path: str) -> None:
    """A prompt that didn't come from a Slack delivery IS mirrored."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "what's up"})
    await worker.stop()

    assert slack.posts == [("TOP.1", "[Claude] 👤 what's up")]
    reg.close()


@pytest.mark.asyncio
async def test_worker_skips_unbound_session(tmp_db_path: str) -> None:
    """If the session has no thread (not /rn'd or auto-recovered), buffer or skip."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13", agent="claude")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hi"})
    await worker.stop()

    # No thread yet → no Slack post. Event log holds it for replay.
    assert slack.posts == []
    reg.close()


@pytest.mark.asyncio
async def test_notification_is_marked_resolved_on_next_prompt(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue(
        {
            "kind": "notification",
            "message": "Claude waiting",
            "tool_request": 'Bash({"command":"ls"})',
        }
    )
    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "go ahead"})
    await worker.stop()

    # We posted the notification, then the prompt, then EDITED the notification.
    assert len(slack.posts) == 2  # notification + prompt
    assert len(slack.edits) == 1
    edited_ts, edited_text = slack.edits[0]
    assert "resolved" in edited_text
    assert "Claude waiting" in edited_text
    reg.close()


@pytest.mark.asyncio
async def test_notification_is_marked_resolved_on_response(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "notification", "message": "needs input"})
    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "done"})
    await worker.stop()

    assert len(slack.edits) == 1
    assert "resolved" in slack.edits[0][1]
    reg.close()


@pytest.mark.asyncio
async def test_notification_is_marked_resolved_on_slack_reply(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "notification", "message": "approve?"})
    await worker.enqueue({"kind": "slack_reply", "text": "1", "msg_ts": "MSG.1"})
    await worker.stop()

    assert len(slack.edits) == 1
    assert "resolved" in slack.edits[0][1]
    reg.close()


@pytest.mark.asyncio
async def test_back_to_back_notifications_resolve_previous(tmp_db_path: str) -> None:
    """When a second notification arrives, the first must be marked resolved
    and the second becomes the new pending row."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "notification", "message": "first?"})
    await worker.enqueue({"kind": "notification", "message": "second?"})
    await worker.stop()

    # Two notifications posted, the first one edited as resolved.
    assert len(slack.posts) == 2
    first_ts = "thr.1"
    second_ts = "thr.2"
    assert len(slack.edits) == 1
    edited_ts, edited_text = slack.edits[0]
    assert edited_ts == first_ts
    assert "resolved" in edited_text
    assert "first?" in edited_text

    # The second notification is now the pending one.
    pending = reg.consume_pending_notification("s1")
    assert pending is not None
    assert pending["ts"] == second_ts
    assert "second?" in pending["text"]
    reg.close()


@pytest.mark.asyncio
async def test_no_pending_notification_is_a_noop(tmp_db_path: str) -> None:
    """A prompt without a preceding notification should not call edit."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    slack = FakeSlackIO()
    worker = Worker(sid="s1", reg=reg, slack=slack, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "hi"})
    await worker.stop()

    assert slack.edits == []
    reg.close()
