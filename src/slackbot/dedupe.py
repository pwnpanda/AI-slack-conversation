"""In-memory dedupe so prompts that originated from a Slack→pane delivery
are not re-mirrored back to Slack by the UserPromptSubmit hook."""

from __future__ import annotations

import time
from collections import deque


class DeliveryDedupe:
    """Marks recently-delivered (session_id, text) pairs with a TTL.

    `consume(sid, text)` returns True exactly once if a matching mark exists
    within the TTL window, removing it. Otherwise False.
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._entries: deque[tuple[float, str, str]] = deque()

    def mark(self, session_id: str, text: str) -> None:
        self._prune()
        self._entries.append((time.monotonic(), session_id, text))

    def consume(self, session_id: str, text: str) -> bool:
        self._prune()
        for i, (_, sid, t) in enumerate(self._entries):
            if sid == session_id and t == text:
                del self._entries[i]
                return True
        return False

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        while self._entries and self._entries[0][0] < cutoff:
            self._entries.popleft()
