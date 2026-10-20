import asyncio
from dataclasses import dataclass, field

import pytest

from slackbot.deletion import ThreadDeleter, delete_thread
from slackbot.registry import Registry


@dataclass
class FakeMatrix:
    ids_by_thread: dict[str, list[str]] = field(default_factory=dict)
    redacted: list[tuple[str, str]] = field(default_factory=list)
    raise_on: set[str] = field(default_factory=set)

    async def thread_event_ids(self, room_id: str, thread_root: str) -> list[str]:
        return self.ids_by_thread.get(thread_root, [thread_root])

    async def redact(self, room_id: str, event_id: str, reason: str = "deleted") -> None:
        if event_id in self.raise_on:
            raise RuntimeError("boom")
        self.redacted.append((room_id, event_id))


@pytest.mark.asyncio
async def test_delete_thread_redacts_all_and_clears_binding(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "ai", "3", agent="claude", matrix_room_id="!host:server")
    reg.set_name("s1", "ftp")
    reg.set_matrix_thread_root("s1", "$root:server")
    matrix = FakeMatrix(
        ids_by_thread={"$root:server": ["$c1:server", "$c2:server", "$root:server"]}
    )
    n = await delete_thread(matrix, reg, "!host:server", "$root:server")
    assert n == 3
    assert matrix.redacted == [
        ("!host:server", "$c1:server"),
        ("!host:server", "$c2:server"),
        ("!host:server", "$root:server"),
    ]
    # Binding cleared so a live session starts a fresh top-level next time.
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.name is None
    assert sess.matrix_thread_root is None
    reg.close()


@pytest.mark.asyncio
async def test_delete_thread_counts_only_successful_redactions(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = FakeMatrix(
        ids_by_thread={"$root:server": ["$c1:server", "$c2:server", "$root:server"]},
        raise_on={"$c2:server"},
    )
    n = await delete_thread(matrix, reg, "!host:server", "$root:server")
    # One redaction raised; the other two still count and don't abort the loop.
    assert n == 2
    assert ("!host:server", "$root:server") in matrix.redacted
    reg.close()


@pytest.mark.asyncio
async def test_delete_thread_no_session_is_fine(tmp_db_path: str) -> None:
    """Deleting a thread that maps to no registry session (already cleared,
    or a stray root) still redacts and doesn't raise."""
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = FakeMatrix(ids_by_thread={"$root:server": ["$root:server"]})
    n = await delete_thread(matrix, reg, "!host:server", "$root:server")
    assert n == 1
    reg.close()


@dataclass
class GatedMatrix(FakeMatrix):
    """Blocks thread_event_ids until released, to observe background running."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def thread_event_ids(self, room_id: str, thread_root: str) -> list[str]:
        self.calls += 1
        await self.gate.wait()
        if thread_root in self.raise_on:
            raise RuntimeError("history fetch failed")
        return [thread_root]


@pytest.mark.asyncio
async def test_thread_deleter_runs_in_background(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = GatedMatrix()
    deleter = ThreadDeleter(matrix, reg)
    assert deleter.start("!host:server", "$root:server") is True
    await asyncio.sleep(0)
    # start() returned while the deletion is still waiting on the server.
    assert matrix.redacted == []
    matrix.gate.set()
    await deleter.wait_idle()
    assert matrix.redacted == [("!host:server", "$root:server")]
    reg.close()


@pytest.mark.asyncio
async def test_thread_deleter_ignores_repeat_while_running(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = GatedMatrix()
    deleter = ThreadDeleter(matrix, reg)
    assert deleter.start("!host:server", "$root:server") is True
    assert deleter.start("!host:server", "$root:server") is False
    matrix.gate.set()
    await deleter.wait_idle()
    assert matrix.calls == 1
    # Once finished, the same thread can be deleted again.
    assert deleter.start("!host:server", "$root:server") is True
    await deleter.wait_idle()
    reg.close()


@pytest.mark.asyncio
async def test_thread_deleter_logs_failures_without_raising(
    tmp_db_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    matrix = GatedMatrix(raise_on={"$root:server"})
    matrix.gate.set()
    deleter = ThreadDeleter(matrix, reg)
    deleter.start("!host:server", "$root:server")
    await deleter.wait_idle()
    assert "failed to delete thread $root:server" in caplog.text
    reg.close()
