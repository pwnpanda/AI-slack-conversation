"""Pure functions that turn structured events into Slack message text."""

from __future__ import annotations

import re
from typing import Any

_RN_PATTERN = re.compile(r"^(?:(?:[/#!](?:rn|rename|register))|rn)\s+(\S+)\s*$")

# Matrix events have a hard size ceiling (max_request_size, server-side
# typically 64 KiB). We leave headroom for the JSON envelope, m.relates_to
# fields, agent prefix, etc. Anything past this gets chunked across multiple
# messages — never silently dropped. Single-line bodies are not chunked,
# only multi-line ones where a sane split is obvious.
_CHUNK_AT = 60_000
# tool_request is the raw JSON for the tool the agent wants to run. It can be
# enormous (entire file contents for an Edit, etc.) and would dominate the
# notification message without adding information beyond what's in the
# message line above it. Capped softly with an ellipsis hint.
_TOOL_REQUEST_CAP = 1200


def agent_label(agent: str | None) -> str:
    labels = {
        "claude": "Claude",
        "codex": "Codex",
        "gemini": "Gemini",
    }
    return labels.get((agent or "claude").lower(), "Claude")


def top_level_text(name: str, cwd: str, status: str, agent: str | None = None) -> str:
    prefix = f"[{agent_label(agent)}] "
    if status == "active":
        return f"🟢 {prefix}{name}  ·  {cwd}"
    return f"⚪ {prefix}{name}  ·  {cwd}  (ended)"


def _codeblock_if_multiline(text: str) -> str:
    if "\n" in text:
        return f"\n```\n{text}\n```"
    return f" {text}"


def format_event(kind: str, data: dict[str, Any]) -> str:
    prefix = f"[{agent_label(str(data.get('agent', 'claude')))}] "
    if kind == "prompt":
        return f"{prefix}👤{_codeblock_if_multiline(str(data.get('text', '')))}"
    if kind == "response":
        body = f"{prefix}🤖{_codeblock_if_multiline(str(data.get('text', '')))}"
        summary = data.get("tool_summary")
        if summary:
            body += f"\n_↳ {summary}_"
        return body
    if kind == "notification":
        msg = str(data.get("message", "")) or "waiting for input"
        parts = [f"{prefix}⏸ {msg}"]
        tool_request = str(data.get("tool_request", "")).strip()
        if tool_request:
            tr = (
                tool_request
                if len(tool_request) <= _TOOL_REQUEST_CAP
                else tool_request[:_TOOL_REQUEST_CAP] + "…"
            )
            parts.append(f"_Asking permission for:_ `{tr}`")
            parts.append("_Reply `1` to approve, `2` to deny, `3` to allow for session._")
        ctx = str(data.get("context", "")).strip()
        if ctx:
            parts.append(f"```\n{ctx}\n```")
        return "\n".join(parts)
    if kind == "error":
        return f"{prefix}❌ {data.get('text', '')}"
    return f"{prefix}[{kind}] {data!r}"


def chunk_for_matrix(text: str, limit: int = _CHUNK_AT) -> list[str]:
    """Split *text* across Matrix-event-sized chunks without losing content.

    Returns a single-element list when the text is already under *limit*.
    For longer text, prefers splitting at newline boundaries; falls back
    to a hard slice if a single line is longer than *limit*. Each chunk
    after the first is prefixed with `…` so it's obvious in the thread
    that the content is a continuation.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    total = len(chunks)
    return [c if i == 0 else f"…(part {i + 1}/{total})\n{c}" for i, c in enumerate(chunks)]


def parse_rn_command(prompt_text: str) -> str | None:
    """Return the name argument if `prompt_text` is a rename command."""
    m = _RN_PATTERN.match(prompt_text.strip())
    return m.group(1) if m else None
