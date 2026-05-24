"""Pure functions that turn structured events into Slack message text."""

from __future__ import annotations

import re
from typing import Any

_RN_PATTERN = re.compile(r"^/(?:rn|rename|register)\s+(\S+)\s*$")
_TRUNCATE_AT = 3000


def top_level_text(name: str, cwd: str, status: str) -> str:
    if status == "active":
        return f"🟢 {name}  ·  {cwd}"
    return f"⚪ {name}  ·  {cwd}  (ended)"


def _codeblock_if_multiline(text: str) -> str:
    if "\n" in text:
        truncated = text if len(text) <= _TRUNCATE_AT else text[:_TRUNCATE_AT] + "\n…[truncated]"
        return f"\n```\n{truncated}\n```"
    return f" {text}"


def format_event(kind: str, data: dict[str, Any]) -> str:
    if kind == "prompt":
        return f"👤{_codeblock_if_multiline(str(data.get('text', '')))}"
    if kind == "response":
        body = f"🤖{_codeblock_if_multiline(str(data.get('text', '')))}"
        summary = data.get("tool_summary")
        if summary:
            body += f"\n_↳ {summary}_"
        return body
    if kind == "notification":
        return f"⏸ {data.get('message', '')}"
    if kind == "error":
        return f"❌ {data.get('text', '')}"
    return f"[{kind}] {data!r}"


def parse_rn_command(prompt_text: str) -> str | None:
    """Return the name argument if `prompt_text` is an /rn-style command."""
    m = _RN_PATTERN.match(prompt_text.strip())
    return m.group(1) if m else None
