from dataclasses import dataclass, field

import pytest

from slackbot.registry import Registry
from slackbot.slack_commands import SlackCommandHandler


@dataclass
class _FakeSlack:
    in_thread: list[tuple[str, str, str]] = field(default_factory=list)  # (thread, text, ch)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)  # (ts, emoji, ch)
    _ts: int = 100

    async def post_in_thread(self, thread_ts: str, text: str, channel: str | None = None) -> str:
        self._ts += 1
        self.in_thread.append((thread_ts, text, channel or ""))
        return f"thr.{self._ts}"

    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None:
        self.reactions.append((ts, emoji, channel or ""))


@dataclass
class _FakeActuator:
    spawns: list[dict] = field(default_factory=list)
    raise_on_spawn: Exception | None = None

    async def spawn_pane_with_command(
        self, session, command_argv, initial_text, delay_seconds
    ) -> None:
        self.spawns.append(
            {
                "session": session,
                "command_argv": tuple(command_argv),
                "initial_text": initial_text,
                "delay_seconds": delay_seconds,
            }
        )
        if self.raise_on_spawn is not None:
            raise self.raise_on_spawn


def _handler(reg, slack, actuator=None):
    return SlackCommandHandler(
        reg=reg,
        slack=slack,
        actuator=actuator or _FakeActuator(),
        zellij_session="main",
        new_pane_command=("claude", "--dangerously-skip-permissions"),
        new_pane_delay_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_new_spawns_pane_and_types_rn_when_name_free(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    slack = _FakeSlack()
    actuator = _FakeActuator()
    cmd = _handler(reg, slack, actuator)
    handled = await cmd.maybe_handle(channel="C-X", text="/new kbd", msg_ts="MSG.1")
    assert handled is True
    assert len(actuator.spawns) == 1
    spawn = actuator.spawns[0]
    assert spawn["session"] == "main"
    assert spawn["command_argv"] == ("claude", "--dangerously-skip-permissions")
    assert spawn["initial_text"] == "/rn kbd"
    # Hourglass before, checkmark after.
    emojis = [r[1] for r in slack.reactions if r[0] == "MSG.1"]
    assert "hourglass_flowing_sand" in emojis
    assert "white_check_mark" in emojis
    reg.close()


@pytest.mark.asyncio
async def test_new_refuses_when_name_already_in_use(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("existing", "/x", "main", "1", slack_channel="C-X")
    reg.claim_name("existing", "kbd")
    reg.set_thread_ts("existing", "T.OLD")
    slack = _FakeSlack()
    actuator = _FakeActuator()
    cmd = _handler(reg, slack, actuator)
    handled = await cmd.maybe_handle(channel="C-X", text="/new kbd", msg_ts="MSG.2")
    assert handled is True
    assert actuator.spawns == []  # no pane spawned on collision
    assert any("already in use" in t[1] for t in slack.in_thread)
    assert ("MSG.2", "x", "C-X") in slack.reactions
    reg.close()


@pytest.mark.asyncio
async def test_handler_ignores_non_command_text(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    cmd = _handler(reg, _FakeSlack())
    assert await cmd.maybe_handle(channel="C-X", text="hello there", msg_ts="m") is False
    assert await cmd.maybe_handle(channel="C-X", text="/new", msg_ts="m") is False
    assert await cmd.maybe_handle(channel="C-X", text="/newx foo", msg_ts="m") is False
    reg.close()


@pytest.mark.asyncio
async def test_spawn_failure_is_reported(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    slack = _FakeSlack()
    actuator = _FakeActuator(raise_on_spawn=RuntimeError("zellij not running"))
    cmd = _handler(reg, slack, actuator)
    await cmd.maybe_handle(channel="C-X", text="/new kbd", msg_ts="MSG.3")
    assert any("Failed to spawn new pane" in t[1] for t in slack.in_thread)
    # Hourglass reaction set, but no success checkmark.
    emojis = [r[1] for r in slack.reactions if r[0] == "MSG.3"]
    assert "hourglass_flowing_sand" in emojis
    assert "white_check_mark" not in emojis
    reg.close()


@pytest.mark.asyncio
async def test_name_uniqueness_is_per_channel(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("existing", "/x", "main", "1", slack_channel="C-A")
    reg.claim_name("existing", "kbd")
    cmd = _handler(reg, _FakeSlack(), _FakeActuator())
    # Same name in a different channel should be allowed.
    assert await cmd.maybe_handle(channel="C-B", text="/new kbd", msg_ts="m") is True
    reg.close()
