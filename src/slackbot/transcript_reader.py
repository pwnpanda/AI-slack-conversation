"""Parse CC's JSONL transcript and yield prompt/response events for the worker.

Designed for polling (`drain()`) so tests are clock-free. Production wiring
combines `drain()` with an inotify watch — see Supervisor.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _extract_text(content: Any) -> str:
    """Extract text from Claude and Codex message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {
                "input_text",
                "output_text",
                "text",
            }:
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _extract_tool_summary(content: Any) -> str:
    """Return a brief comma-joined list of tool_use names, or '' if none.

    Without this, an assistant turn whose content is mostly tool_use blocks
    plus a one-line preamble mirrors as just the preamble, losing all context
    about what Claude actually did in that turn.
    """
    if not isinstance(content, list):
        return ""
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return ", ".join(names)


_FINGERPRINT_BYTES = 64


class TranscriptReader:
    """Tail a JSONL file. Holds a byte offset and yields parsed events on drain()."""

    def __init__(self, path: Path | str, start_offset: int | None = None) -> None:
        self._path = Path(path)
        self._offset = 0
        self._fingerprint: bytes = b""
        self._buffer = b""
        self._start_offset = start_offset

    @property
    def offset(self) -> int:
        """Current byte offset into the transcript file."""
        return self._offset

    def open(self) -> None:
        # Default: start from current EOF (we want events appended after we
        # register, not history). When a start_offset is provided (daemon
        # restart with a persisted cursor), seek to that point instead so we
        # replay anything CC wrote while the daemon was down.
        try:
            st = self._path.stat()
            if self._start_offset is not None:
                # Clamp to file size so a stale, oversized offset doesn't read past EOF.
                self._offset = min(self._start_offset, st.st_size)
            else:
                self._offset = st.st_size
            self._fingerprint = self._read_fingerprint()
        except FileNotFoundError:
            self._offset = 0
            self._fingerprint = b""

    def _read_fingerprint(self) -> bytes:
        """Read the first _FINGERPRINT_BYTES bytes of the file as a rotation marker."""
        try:
            with self._path.open("rb") as f:
                return f.read(_FINGERPRINT_BYTES)
        except FileNotFoundError:
            return b""

    def close(self) -> None:
        self._buffer = b""

    def drain(self) -> Iterator[dict[str, Any]]:
        """Read any bytes appended since the last call and yield parsed events."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return
        current_fp = self._read_fingerprint()
        if st.st_size < self._offset or (self._offset > 0 and current_fp != self._fingerprint):
            # File was truncated, rotated, or replaced — restart from the beginning.
            log.info("transcript %s shrank or was replaced; resetting offset", self._path)
            self._offset = 0
            self._fingerprint = current_fp
            self._buffer = b""
        if st.st_size == self._offset:
            return
        with self._path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset += len(chunk)
        self._fingerprint = current_fp
        data = self._buffer + chunk
        # If we don't end on a newline, hold the trailing partial line.
        if not data.endswith(b"\n"):
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                self._buffer = data
                return
            self._buffer = data[last_nl + 1 :]
            data = data[: last_nl + 1]
        else:
            self._buffer = b""
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping malformed transcript line in %s", self._path)
                continue
            event = self._record_to_event(rec)
            if event is not None:
                yield event

    def _record_to_event(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        rtype = rec.get("type")
        if rtype == "response_item":
            payload = rec.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                return None
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                return None
            text = _extract_text(payload.get("content"))
            if not text:
                return None
            return {
                "kind": "prompt" if role == "user" else "response",
                "uuid": payload.get("id"),
                "parentUuid": None,
                "text": text,
            }
        uuid = rec.get("uuid")
        parent = rec.get("parentUuid")
        msg = rec.get("message")
        if rtype == "user" and isinstance(msg, dict):
            content = msg.get("content")
            text = _extract_text(content)
            if not text:
                return None
            return {"kind": "prompt", "uuid": uuid, "parentUuid": parent, "text": text}
        if rtype == "assistant" and isinstance(msg, dict):
            content = msg.get("content")
            text = _extract_text(content)
            if not text:
                return None
            event: dict[str, Any] = {
                "kind": "response",
                "uuid": uuid,
                "parentUuid": parent,
                "text": text,
            }
            tool_summary = _extract_tool_summary(content)
            if tool_summary:
                event["tool_summary"] = tool_summary
            return event
        return None
