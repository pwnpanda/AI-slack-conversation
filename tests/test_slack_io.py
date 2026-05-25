from dataclasses import dataclass, field

import pytest

from slackbot.slack_io import SlackIO


@dataclass
class FakeSlackClient:
    posted: list[dict] = field(default_factory=list)
    edited: list[dict] = field(default_factory=list)
    reacted: list[dict] = field(default_factory=list)
    next_ts: str = "1700000000.000001"

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": self.next_ts}

    async def chat_update(self, **kwargs):
        self.edited.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs):
        self.reacted.append(kwargs)
        return {"ok": True}


@pytest.mark.asyncio
async def test_post_top_level_returns_ts() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1", agent_channels={"codex": "C-CODEX"})
    ts = await io.post_top_level("🟢 myproj", channel=io.channel_for_agent("codex"))
    assert ts == fake.next_ts
    assert fake.posted[0] == {"channel": "C-CODEX", "text": "🟢 myproj"}
    assert io.channel_for_agent("gemini") == "C1"


@pytest.mark.asyncio
async def test_post_in_thread_uses_thread_ts() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    await io.post_in_thread("1.0", "👤 hi")
    assert fake.posted[0] == {"channel": "C1", "text": "👤 hi", "thread_ts": "1.0"}


@pytest.mark.asyncio
async def test_edit_top_level_calls_update() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    await io.edit_top_level("1.0", "⚪ done")
    assert fake.edited[0] == {"channel": "C1", "ts": "1.0", "text": "⚪ done"}


@pytest.mark.asyncio
async def test_react_calls_reactions_add() -> None:
    fake = FakeSlackClient()
    io = SlackIO(fake, channel="C1")
    await io.react("1.0", "white_check_mark")
    assert fake.reacted[0] == {
        "channel": "C1",
        "timestamp": "1.0",
        "name": "white_check_mark",
    }
