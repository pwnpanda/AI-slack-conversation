from dataclasses import dataclass, field

import pytest

from slackbot.handlers import EventHandlers
from slackbot.registry import Registry
from slackbot.supervisor import Supervisor


@dataclass
class FakeMatrixIO:
    top_level_posts: list[str] = field(default_factory=list)
    top_level_rooms: list[str | None] = field(default_factory=list)
    thread_posts: list[tuple[str, str]] = field(default_factory=list)
    thread_rooms: list[str | None] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    edit_rooms: list[str | None] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    _seq: int = 0

    def room_for_agent(self, agent: str) -> str:
        return f"!{agent}:server"

    def has_user_client(self) -> bool:
        return False

    async def post_top_level(
        self, text: str, room_id: str | None = None, as_user: bool = False
    ) -> str:
        self.top_level_posts.append(text)
        self.top_level_rooms.append(room_id)
        self._seq += 1
        return f"$top.{self._seq}"

    async def post_in_thread(
        self, thread_root: str, text: str, room_id: str | None = None, as_user: bool = False
    ) -> str:
        self.thread_posts.append((thread_root, text))
        self.thread_rooms.append(room_id)
        self._seq += 1
        return f"$thr.{self._seq}"

    async def edit_top_level(self, ts: str, text: str, room_id: str | None = None) -> None:
        self.edits.append((ts, text))
        self.edit_rooms.append(room_id)

    async def react(self, ts: str, emoji: str, room_id: str | None = None) -> None:
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


@pytest.fixture
def shared_matrix():
    return FakeMatrixIO()


@pytest.fixture
def sup(reg: Registry, shared_matrix: FakeMatrixIO):
    return Supervisor(reg=reg, matrix=shared_matrix, actuator=_NoopActuator())


def _start(sid: str = "s1", cwd: str = "/x", pane: str = "0", agent: str = "claude") -> dict:
    return {
        "kind": "start",
        "session_id": sid,
        "cwd": cwd,
        "zellij_session": "main",
        "zellij_pane_id": pane,
        "agent": agent,
        "resumed": False,
    }


@pytest.mark.asyncio
async def test_claude_start_creates_row(reg: Registry, sup: Supervisor) -> None:
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    await h.handle(_start())
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.agent == "claude"
    assert sess.name is None  # not auto-registered for claude
    await sup.shutdown()


@pytest.mark.asyncio
async def test_codex_start_auto_registers(reg: Registry, sup: Supervisor) -> None:
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    await h.handle(_start(agent="codex"))
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.agent == "codex"
    assert sess.name is not None
    assert sess.name.startswith("codex-")
    assert len(matrix.top_level_posts) == 1
    await sup.shutdown()


@pytest.mark.asyncio
async def test_name_event_posts_top_level(reg: Registry, sup: Supervisor) -> None:
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    assert matrix.top_level_posts == ["🟢 [Claude] myproj  ·  /x"]
    sess = reg.get_session("s1")
    assert sess is not None and sess.name == "myproj"
    assert sess.matrix_thread_root is not None
    await sup.shutdown()


@pytest.mark.asyncio
async def test_rename_edits_top_level(reg: Registry, sup: Supervisor) -> None:
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "old"})
    matrix.edits.clear()
    await h.handle({"kind": "name", "session_id": "s1", "name": "new"})
    # rename when row already has a name flows through claim_name then re-posts divider
    # OR edits top level - either path acceptable, but rename should NOT post a second top-level
    assert len(matrix.top_level_posts) == 1
    sess = reg.get_session("s1")
    assert sess is not None and sess.name == "new"
    await sup.shutdown()


@pytest.mark.asyncio
async def test_end_event_marks_status_ended(reg: Registry, sup: Supervisor) -> None:
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "done"})
    sess = reg.get_session("s1")
    assert sess is not None and sess.status == "ended"
    await sup.shutdown()


@pytest.mark.asyncio
async def test_resumed_start_edits_top_level_back_to_active(reg: Registry, sup: Supervisor) -> None:
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1"})
    matrix.edits.clear()
    # resume: same sid SessionStart
    await h.handle(_start(pane="2"))
    # An active-emoji edit posted (existing prior row had name+thread, so the resume edits)
    assert any("🟢" in t for _, t in matrix.edits)
    await sup.shutdown()


@pytest.mark.asyncio
async def test_notification_enqueues_into_worker(
    reg: Registry, sup: Supervisor, shared_matrix: FakeMatrixIO
) -> None:
    # Handler and supervisor share the same matrix so we can observe worker posts.
    h = EventHandlers(reg, sup, shared_matrix)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "notification", "session_id": "s1", "message": "needs input"})
    # Give worker a tick to drain
    import asyncio

    await asyncio.sleep(0.05)
    # The notification message should have been posted into the thread
    assert any("needs input" in t for _, t in shared_matrix.thread_posts)
    await sup.shutdown()


@pytest.mark.asyncio
async def test_auto_recover_only_when_predecessor_dead(reg: Registry, sup: Supervisor) -> None:
    """Auto-recovery uses session_is_alive — a still-running predecessor is NOT hijacked."""
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    # Set up a "predecessor" — current process counts as 'alive' because session_is_alive
    # with no info returns True. We can't easily simulate a dead predecessor in this test
    # without mocking, so just ensure that an existing-named row in the same cwd is NOT
    # auto-stolen by a new session.
    await h.handle(_start(sid="old", cwd="/proj"))
    await h.handle({"kind": "name", "session_id": "old", "name": "proj-name"})
    matrix.top_level_posts.clear()
    await h.handle(_start(sid="new", cwd="/proj"))
    new_sess = reg.get_session("new")
    assert new_sess is not None
    assert new_sess.name is None  # not hijacked (predecessor still 'alive')
    await sup.shutdown()


@pytest.mark.asyncio
async def test_event_after_ended_self_heals_status_and_reattaches_reader(
    reg: Registry, sup: Supervisor, tmp_path
) -> None:
    """A prompt/notification arriving for a session previously marked ended
    must flip status back to active and re-attach the transcript reader,
    so a stale session_end can't permanently mute the conversation."""
    matrix = FakeMatrixIO()
    h = EventHandlers(reg, sup, matrix)
    transcript = tmp_path / "tx.jsonl"
    transcript.write_text("")
    await h.handle(_start(sid="s1") | {"transcript_path": str(transcript)})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "exit"})
    assert reg.get_session("s1").status == "ended"
    assert "s1" not in sup._readers
    # A fresh hook event arrives — CC must be back.
    await h.handle(
        {
            "kind": "notification",
            "session_id": "s1",
            "message": "Claude needs your permission to use Bash",
        }
    )
    assert reg.get_session("s1").status == "active"
    assert "s1" in sup._readers
