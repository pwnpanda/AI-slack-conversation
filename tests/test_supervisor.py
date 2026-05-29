import pytest

from slackbot.registry import Registry
from slackbot.supervisor import Supervisor


class _FakeSlack:
    def channel_for_agent(self, agent):
        return f"C-{agent.upper()}"

    async def post_top_level(self, text, channel=None):
        return "top.1"

    async def post_in_thread(self, thread_ts, text, channel=None):
        return "thr.1"

    async def edit_top_level(self, ts, text, channel=None):
        pass

    async def react(self, ts, emoji, channel=None):
        pass


class _FakeActuator:
    async def deliver(self, session, pane_id, text):
        pass


@pytest.mark.asyncio
async def test_get_or_create_spawns_one_worker_per_sid(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    sup = Supervisor(reg=reg, slack=_FakeSlack(), actuator=_FakeActuator())
    w1 = await sup.get_or_create("s1")
    w2 = await sup.get_or_create("s1")
    assert w1 is w2
    await sup.shutdown()
    reg.close()


@pytest.mark.asyncio
async def test_reap_removes_idle_workers(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    clock = {"t": 1000.0}
    sup = Supervisor(
        reg=reg,
        slack=_FakeSlack(),
        actuator=_FakeActuator(),
        idle_seconds=5,
        clock=lambda: clock["t"],
    )
    w = await sup.get_or_create("s1")
    # advance the clock past idle window
    clock["t"] += 10.0
    await sup.reap_once()
    assert "s1" not in sup._workers
    assert w._task is None or w._task.done()
    await sup.shutdown()
    reg.close()


@pytest.mark.asyncio
async def test_attached_reader_keeps_worker_alive_indefinitely(tmp_db_path: str, tmp_path) -> None:
    """A worker with an attached reader must NOT be reaped even when the
    reader stays silent — the reader's presence marks the session as still
    being watched, and a quiet CC is not the same as a dead CC."""
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    clock = {"t": 1000.0}
    sup = Supervisor(
        reg=reg,
        slack=_FakeSlack(),
        actuator=_FakeActuator(),
        idle_seconds=5,
        clock=lambda: clock["t"],
    )
    await sup.get_or_create("s1")

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    sup.attach_reader("s1", str(transcript))

    # Far past the idle window, with the reader silent the whole time.
    for _ in range(15):
        clock["t"] += 1.0
        await sup.pump_readers()
    await sup.reap_once()
    assert "s1" in sup._workers, "attached reader should keep the worker alive"

    # Once session_end detaches the reader, the next reaper pass cleans up.
    sup.detach_reader("s1")
    await sup.reap_once()
    assert "s1" not in sup._workers
    await sup.shutdown()
    reg.close()


@pytest.mark.asyncio
async def test_recent_activity_keeps_worker_alive(tmp_db_path: str) -> None:
    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13")
    clock = {"t": 1000.0}
    sup = Supervisor(
        reg=reg,
        slack=_FakeSlack(),
        actuator=_FakeActuator(),
        idle_seconds=5,
        clock=lambda: clock["t"],
    )
    await sup.get_or_create("s1")
    clock["t"] += 2.0
    await sup.touch("s1")
    clock["t"] += 4.0  # 4s after touch — still inside window
    await sup.reap_once()
    assert "s1" in sup._workers
    await sup.shutdown()
    reg.close()
