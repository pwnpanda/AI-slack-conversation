"""Pure functions that turn structured events into Slack message text."""

from __future__ import annotations

import re
from typing import Any

_RN_PATTERN = re.compile(r"^(?:(?:[/#!](?:rn|rename|register))|rn)\s+(\S+)\s*$")
_TRUNCATE_AT = 3000


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
        truncated = text if len(text) <= _TRUNCATE_AT else text[:_TRUNCATE_AT] + "\n…[truncated]"
        return f"\n```\n{truncated}\n```"
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
            # Truncate very long tool_request JSON so Slack stays readable.
            tr = tool_request if len(tool_request) <= 400 else tool_request[:400] + "…"
            parts.append(f"_Asking permission for:_ `{tr}`")
            parts.append("_Reply `1` to approve, `2` to deny, `3` to allow for session._")
        ctx = str(data.get("context", "")).strip()
        if ctx:
            # Show the tail so the question itself is visible without flooding the channel.
            tail = "\n".join(ctx.splitlines()[-6:])
            if tail:
                tail_trunc = tail if len(tail) <= _TRUNCATE_AT else tail[:_TRUNCATE_AT] + "\n…"
                parts.append(f"```\n{tail_trunc}\n```")
        return "\n".join(parts)
    if kind == "error":
        return f"{prefix}❌ {data.get('text', '')}"
    return f"{prefix}[{kind}] {data!r}"


def parse_rn_command(prompt_text: str) -> str | None:
    """Return the name argument if `prompt_text` is a rename command."""
    m = _RN_PATTERN.match(prompt_text.strip())
    return m.group(1) if m else None
