import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from slackbot.registry import Registry
from slackbot.supervisor import Supervisor


@dataclass
class FakeSlack:
    posts: list[tuple[str, str]] = field(default_factory=list)
    top_level: list[str] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    _ts: int = 0

    def channel_for_agent(self, agent):
        return f"C-{agent.upper()}"

    async def post_top_level(self, text, channel=None):
        self.top_level.append(text)
        self._ts += 1
        return f"top.{self._ts}"

    async def post_in_thread(self, thread_ts, text, channel=None):
        self.posts.append((thread_ts, text))
        self._ts += 1
        return f"thr.{self._ts}"

    async def edit_top_level(self, ts, text, channel=None):
        pass

    async def react(self, ts, emoji, channel=None):
        self.reacts.append((ts, emoji))


class FakeActuator:
    def __init__(self):
        self.deliveries = []

    async def deliver(self, session, pane_id, text):
        self.deliveries.append((session, pane_id, text))


@pytest.mark.asyncio
async def test_full_turn_mirrored_via_transcript(tmp_path: Path, tmp_db_path: str) -> None:
    transcript = tmp_path / "tx.jsonl"
    transcript.write_text("")

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session(
        "s1",
        "/x",
        "main",
        "13",
        agent="claude",
        slack_channel="C-CLAUDE",
        transcript_path=str(transcript),
    )
    reg.set_name("s1", "myproj")
    reg.set_thread_ts("s1", "TOP.1")

    slack = FakeSlack()
    sup = Supervisor(reg=reg, slack=slack, actuator=FakeActuator())
    sup.attach_reader("s1", str(transcript))
    await sup.get_or_create("s1")

    # Simulate CC writing a user message + assistant message to the transcript.
    with transcript.open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u1",
                    "parentUuid": None,
                    "message": {"role": "user", "content": "hi"},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "parentUuid": "u1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "hello back"}],
                    },
                }
            )
            + "\n"
        )

    await sup.pump_readers()
    # Give the worker a tick to drain its queue.
    await asyncio.sleep(0.05)
    await sup.shutdown()

    # Both messages mirrored exactly once into the bound thread.
    assert ("TOP.1", "[Claude] 👤 hi") in slack.posts
    assert ("TOP.1", "[Claude] 🤖 hello back") in slack.posts
    assert len(slack.posts) == 2
    reg.close()


@pytest.mark.asyncio
async def test_slack_reply_delivers_and_suppresses_echo(tmp_path: Path, tmp_db_path: str) -> None:
    transcript = tmp_path / "tx.jsonl"
    transcript.write_text("")
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session(
        "s1",
        "/x",
        "main",
        "13",
        agent="claude",
        slack_channel="C-CLAUDE",
        transcript_path=str(transcript),
    )
    reg.set_name("s1", "myproj")
    reg.set_thread_ts("s1", "TOP.1")

    slack = FakeSlack()
    actuator = FakeActuator()
    sup = Supervisor(reg=reg, slack=slack, actuator=actuator)
    sup.attach_reader("s1", str(transcript))
    worker = await sup.get_or_create("s1")

    # Slack reply: should deliver to pane and queue an echo suppression.
    await worker.enqueue({"kind": "slack_reply", "text": "ping", "msg_ts": "MSG.1"})
    # Transcript writes the user message a moment later (CC accepted the typed text).
    with transcript.open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u_x",
                    "parentUuid": None,
                    "message": {"role": "user", "content": "ping"},
                }
            )
            + "\n"
        )
    await asyncio.sleep(0.05)
    await sup.pump_readers()
    await asyncio.sleep(0.05)
    await sup.shutdown()

    assert actuator.deliveries == [("main", "13", "ping")]
    assert ("MSG.1", "white_check_mark") in slack.reacts
    # The echoed user message must NOT have been mirrored back into Slack.
    assert slack.posts == []
    reg.close()
