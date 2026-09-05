from dataclasses import dataclass, field

import pytest

from slackbot.deletion import delete_thread
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
