import pytest

from slackbot.liveness_cache import LivenessCache


@pytest.mark.asyncio
async def test_cache_hit_returns_memoized() -> None:
    calls: list[tuple] = []

    def probe(sid, pid, name):
        calls.append((sid, pid, name))
        return True

    clock = {"t": 1000.0}
    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: clock["t"])
    assert await cache.is_alive("s1", 100, "n") is True
    assert await cache.is_alive("s1", 100, "n") is True  # cache hit, no second call
    assert calls == [("s1", 100, "n")]


@pytest.mark.asyncio
async def test_cache_expires_after_ttl() -> None:
    calls: list[tuple] = []

    def probe(sid, pid, name):
        calls.append((sid, pid, name))
        return True

    clock = {"t": 1000.0}
    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: clock["t"])
    await cache.is_alive("s1", 100, "n")
    clock["t"] += 11.0
    await cache.is_alive("s1", 100, "n")
    assert len(calls) == 2  # ttl expired → re-probed


@pytest.mark.asyncio
async def test_cache_keyed_on_full_tuple() -> None:
    calls: list[tuple] = []

    def probe(sid, pid, name):
        calls.append((sid, pid, name))
        return True

    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: 1000.0)
    await cache.is_alive("s1", 100, "n")
    await cache.is_alive("s2", 100, "n")
    await cache.is_alive("s1", 101, "n")
    assert len(calls) == 3  # each unique tuple probed once


@pytest.mark.asyncio
async def test_probe_runs_off_event_loop() -> None:
    """Confirm probe runs via asyncio.to_thread (i.e. doesn't block)."""
    import threading

    main_thread_id = threading.get_ident()
    captured: dict = {}

    def probe(sid, pid, name):
        captured["thread"] = threading.get_ident()
        return True

    cache = LivenessCache(probe, ttl_seconds=10, clock=lambda: 1000.0)
    await cache.is_alive("s", 1, "n")
    assert captured["thread"] != main_thread_id
