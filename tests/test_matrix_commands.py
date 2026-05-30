from dataclasses import dataclass, field

import pytest

from slackbot.matrix_commands import MatrixCommandHandler
from slackbot.registry import Registry


@dataclass
class _FakeMatrix:
    in_thread: list[tuple[str, str, str]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    _seq: int = 100

    async def post_in_thread(self, thread_root: str, text: str, room_id: str | None = None) -> str:
        self._seq += 1
        self.in_thread.append((thread_root, text, room_id or ""))
        return f"$thr.{self._seq}"

    async def react(self, ts: str, emoji: str, room_id: str | None = None) -> None:
        self.reactions.append((ts, emoji, room_id or ""))


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


def _handler(reg, matrix, actuator=None):
    return MatrixCommandHandler(
        reg=reg,
        matrix=matrix,
        actuator=actuator or _FakeActuator(),
        zellij_session="main",
        new_pane_command=("claude", "--dangerously-skip-permissions"),
        new_pane_delay_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_new_spawns_pane_and_types_rn_when_name_free(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = _FakeMatrix()
    actuator = _FakeActuator()
    cmd = _handler(reg, matrix, actuator)
    handled = await cmd.maybe_handle(room_id="!x:server", text="/new kbd", msg_ts="$MSG1:server")
    assert handled is True
    assert len(actuator.spawns) == 1
    spawn = actuator.spawns[0]
    assert spawn["session"] == "main"
    assert spawn["command_argv"] == ("claude", "--dangerously-skip-permissions")
    assert spawn["initial_text"] == "/rn kbd"
    # Hourglass before, checkmark after.
    emojis = [r[1] for r in matrix.reactions if r[0] == "$MSG1:server"]
    assert "hourglass_flowing_sand" in emojis
    assert "white_check_mark" in emojis
    reg.close()


@pytest.mark.asyncio
async def test_new_refuses_when_name_already_in_use(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("existing", "/x", "main", "1", matrix_room_id="!x:server")
    reg.claim_name("existing", "kbd")
    reg.set_matrix_thread_root("existing", "$TOLD:server")
    matrix = _FakeMatrix()
    actuator = _FakeActuator()
    cmd = _handler(reg, matrix, actuator)
    handled = await cmd.maybe_handle(room_id="!x:server", text="/new kbd", msg_ts="$MSG2:server")
    assert handled is True
    assert actuator.spawns == []  # no pane spawned on collision
    assert any("already in use" in t[1] for t in matrix.in_thread)
    assert ("$MSG2:server", "x", "!x:server") in matrix.reactions
    reg.close()


@pytest.mark.asyncio
async def test_handler_ignores_non_command_text(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    cmd = _handler(reg, _FakeMatrix())
    assert await cmd.maybe_handle(room_id="!x:server", text="hello there", msg_ts="m") is False
    assert await cmd.maybe_handle(room_id="!x:server", text="/new", msg_ts="m") is False
    assert await cmd.maybe_handle(room_id="!x:server", text="/newx foo", msg_ts="m") is False
    reg.close()


@pytest.mark.asyncio
async def test_spawn_failure_is_reported(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = _FakeMatrix()
    actuator = _FakeActuator(raise_on_spawn=RuntimeError("zellij not running"))
    cmd = _handler(reg, matrix, actuator)
    await cmd.maybe_handle(room_id="!x:server", text="/new kbd", msg_ts="$MSG3:server")
    assert any("Failed to spawn new pane" in t[1] for t in matrix.in_thread)
    # Hourglass reaction set, but no success checkmark.
    emojis = [r[1] for r in matrix.reactions if r[0] == "$MSG3:server"]
    assert "hourglass_flowing_sand" in emojis
    assert "white_check_mark" not in emojis
    reg.close()


@pytest.mark.asyncio
async def test_name_uniqueness_is_per_room(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("existing", "/x", "main", "1", matrix_room_id="!a:server")
    reg.claim_name("existing", "kbd")
    cmd = _handler(reg, _FakeMatrix(), _FakeActuator())
    # Same name in a different room should be allowed.
    assert await cmd.maybe_handle(room_id="!b:server", text="/new kbd", msg_ts="m") is True
    reg.close()


@pytest.mark.asyncio
async def test_resume_spawns_pane_with_resume_flag(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = _FakeMatrix()
    actuator = _FakeActuator()
    cmd = _handler(reg, matrix, actuator)
    handled = await cmd.maybe_handle(room_id="!claude:s", text="/resume Finance", msg_ts="$M1")
    assert handled is True
    assert len(actuator.spawns) == 1
    spawn = actuator.spawns[0]
    assert spawn["command_argv"][-2:] == ("--resume", "Finance")
    # initial_text empty: CC starts directly, no typing needed.
    assert spawn["initial_text"] == ""
    # Both hourglass and checkmark on the user's command message.
    emojis = [r[1] for r in matrix.reactions if r[0] == "$M1"]
    assert "hourglass_flowing_sand" in emojis
    assert "white_check_mark" in emojis
    reg.close()


@pytest.mark.asyncio
async def test_resume_spawn_failure_is_reported(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = _FakeMatrix()
    actuator = _FakeActuator(raise_on_spawn=RuntimeError("zellij ai not running"))
    cmd = _handler(reg, matrix, actuator)
    await cmd.maybe_handle(room_id="!claude:s", text="/resume Finance", msg_ts="$M2")
    assert any("Failed to spawn resume pane" in t[1] for t in matrix.in_thread)
    emojis = [r[1] for r in matrix.reactions if r[0] == "$M2"]
    assert "white_check_mark" not in emojis
    reg.close()


@pytest.mark.asyncio
async def test_resume_pattern_rejects_extra_args(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    cmd = _handler(reg, _FakeMatrix(), _FakeActuator())
    assert await cmd.maybe_handle(room_id="!c:s", text="/resume", msg_ts="m") is False
    assert await cmd.maybe_handle(room_id="!c:s", text="/resumex Finance", msg_ts="m") is False
    reg.close()
