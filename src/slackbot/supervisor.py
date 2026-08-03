"""Worker lifecycle: spawn on first event, reap on idle."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from slackbot.registry import Registry
from slackbot.transcript_reader import TranscriptReader
from slackbot.worker import Worker

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(
        self,
        reg: Registry,
        matrix,
        actuator,
        idle_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reg = reg
        self._matrix = matrix
        self._actuator = actuator
        self._idle = idle_seconds
        self._clock = clock
        self._workers: dict[str, Worker] = {}
        self._last_touch: dict[str, float] = {}
        self._readers: dict[str, TranscriptReader] = {}

    async def get_or_create(self, sid: str) -> Worker:
        """Return the worker for *sid*, creating and starting one if needed."""
        worker = self._workers.get(sid)
        if worker is None:
            worker = Worker(sid=sid, reg=self._reg, matrix=self._matrix, actuator=self._actuator)
            await worker.start()
            self._workers[sid] = worker
        self._last_touch[sid] = self._clock()
        return worker

    async def touch(self, sid: str) -> None:
        """Record activity for *sid* so the reaper keeps it alive."""
        if sid in self._workers:
            self._last_touch[sid] = self._clock()

    def attach_reader(
        self, sid: str, transcript_path: str, start_offset: int | None = None
    ) -> None:
        """Open a TranscriptReader for *sid* if one is not already attached.

        When *start_offset* is provided (daemon restart with a persisted
        cursor), the reader resumes from that byte offset instead of snapping
        to EOF, so prompts/responses CC wrote while the daemon was down are
        replayed.
        """
        if sid in self._readers:
            return
        reader = TranscriptReader(Path(transcript_path), start_offset=start_offset)
        reader.open()
        self._readers[sid] = reader

    def has_reader(self, sid: str) -> bool:
        """True when a TranscriptReader is already mirroring *sid*."""
        return sid in self._readers

    def detach_reader(self, sid: str) -> None:
        """Close and remove the TranscriptReader for *sid*."""
        reader = self._readers.pop(sid, None)
        if reader is not None:
            reader.close()

    async def pump_readers(self) -> None:
        """Drain every reader once and enqueue events into the right worker.

        Called periodically (or driven by inotify in production). Keeping it
        polling-shaped makes tests deterministic.
        """
        for sid, reader in list(self._readers.items()):
            events = list(reader.drain())
            if not events:
                continue
            worker = await self.get_or_create(sid)
            for event in events:
                await worker.enqueue(event)
            # Persist the cursor so a daemon restart can resume from here
            # instead of snapping to EOF and silently dropping pending events.
            self._reg.set_transcript_offset(sid, reader.offset)

    async def reap_once(self) -> None:
        """Stop and remove workers that have been idle past *idle_seconds*.

        Workers with an attached transcript reader are never reaped: the
        reader is what marks the session as "still being watched". A quiet
        CC session produces no transcript events but is still alive, and
        reaping it here would orphan subsequent prompts and responses.
        Readers are detached by `_on_end` (session_end hook) or shutdown.
        """
        now = self._clock()
        for sid in list(self._workers.keys()):
            if sid in self._readers:
                continue
            last = self._last_touch.get(sid, now)
            if now - last >= self._idle:
                worker = self._workers.pop(sid)
                self._last_touch.pop(sid, None)
                await worker.stop()
                log.info("reaped idle worker %s", sid)

    async def shutdown(self) -> None:
        """Stop all workers and close all readers for a clean exit."""
        for sid in list(self._workers.keys()):
            worker = self._workers.pop(sid)
            self.detach_reader(sid)
            await worker.stop()
