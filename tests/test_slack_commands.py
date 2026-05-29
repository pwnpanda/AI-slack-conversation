from dataclasses import dataclass, field

import pytest

from slackbot.registry import Registry
from slackbot.slack_commands import SlackCommandHandler


@dataclass
class _FakeSlack:
    top_level: list[tuple[str, str]] = field(default_factory=list)  # (channel, text)
    in_thread: list[tuple[str, str, str]] = field(default_factory=list)  # (thread, text, ch)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)  # (ts, emoji, ch)
    _ts: int = 100

    async def post_top_level(self, text: str, channel: str | None = None) -> str:
        self._ts += 1
        self.top_level.append((channel or "", text))
        return f"top.{self._ts}"

    async def post_in_thread(self, thread_ts: str, text: str, channel: str | None = None) -> str:
        self._ts += 1
        self.in_thread.append((thread_ts, text, channel or ""))
        return f"thr.{self._ts}"

    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None:
        self.reactions.append((ts, emoji, channel or ""))


@pytest.mark.asyncio
async def test_new_reserves_thread_when_name_is_free(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    slack = _FakeSlack()
    cmd = SlackCommandHandler(reg=reg, slack=slack)
    handled = await cmd.maybe_handle(channel="C-X", text="/new kbd", msg_ts="MSG.1")
    assert handled is True
    assert len(slack.top_level) == 1
    assert "kbd" in slack.top_level[0][1]
    sess = reg.get_session_by_name("kbd", channel="C-X")
    assert sess is not None
    assert sess.slack_thread_ts == slack.top_level[0][1] or sess.slack_thread_ts.startswith("top.")
    # Confirmation reaction on the user's command message.
    assert ("MSG.1", "white_check_mark", "C-X") in slack.reactions
    reg.close()


@pytest.mark.asyncio
async def test_new_refuses_when_name_already_in_use(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("existing", "/x", "main", "1", slack_channel="C-X")
    reg.claim_name("existing", "kbd")
    reg.set_thread_ts("existing", "T.OLD")
    slack = _FakeSlack()
    cmd = SlackCommandHandler(reg=reg, slack=slack)
    handled = await cmd.maybe_handle(channel="C-X", text="/new kbd", msg_ts="MSG.2")
    assert handled is True
    assert slack.top_level == []  # no new thread created
    assert any("already in use" in t[1] for t in slack.in_thread)
    assert ("MSG.2", "x", "C-X") in slack.reactions
    reg.close()


@pytest.mark.asyncio
async def test_handler_ignores_non_command_text(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    cmd = SlackCommandHandler(reg=reg, slack=_FakeSlack())
    assert await cmd.maybe_handle(channel="C-X", text="hello there", msg_ts="m") is False
    assert await cmd.maybe_handle(channel="C-X", text="/new", msg_ts="m") is False
    assert await cmd.maybe_handle(channel="C-X", text="/newx foo", msg_ts="m") is False
    reg.close()


@pytest.mark.asyncio
async def test_reserved_name_scoped_per_channel(tmp_db_path: str) -> None:
    """Same name in a different channel is allowed (per-agent rooms)."""
    reg = Registry(tmp_db_path)
    reg.open()
    slack = _FakeSlack()
    cmd = SlackCommandHandler(reg=reg, slack=slack)
    assert await cmd.maybe_handle(channel="C-A", text="/new kbd", msg_ts="m1") is True
    assert await cmd.maybe_handle(channel="C-B", text="/new kbd", msg_ts="m2") is True
    assert len(slack.top_level) == 2
    reg.close()
