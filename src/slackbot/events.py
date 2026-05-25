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
        return f"{prefix}⏸ {data.get('message', '')}"
    if kind == "error":
        return f"{prefix}❌ {data.get('text', '')}"
    return f"{prefix}[{kind}] {data!r}"


def parse_rn_command(prompt_text: str) -> str | None:
    """Return the name argument if `prompt_text` is a rename command."""
    m = _RN_PATTERN.match(prompt_text.strip())
    return m.group(1) if m else None
