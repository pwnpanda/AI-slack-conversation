"""TTL-cached liveness probe that runs the /proc scan off the event loop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

ProbeFn = Callable[[str | None, int | None, str | None], bool]
ClockFn = Callable[[], float]


class LivenessCache:
    def __init__(
        self,
        probe: ProbeFn,
        ttl_seconds: float = 10.0,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self._probe = probe
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str | None, int | None, str | None], tuple[float, bool]] = {}

    async def is_alive(
        self, cc_session_id: str | None, cc_pid: int | None, name: str | None
    ) -> bool:
        key = (cc_session_id, cc_pid, name)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None:
            ts, value = cached
            if now - ts < self._ttl:
                return value
        result = await asyncio.to_thread(self._probe, cc_session_id, cc_pid, name)
        self._cache[key] = (now, result)
        return result
