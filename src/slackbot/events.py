"""Pure functions that turn structured events into Matrix message text."""

from __future__ import annotations

import json
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


def top_level_text(
    name: str,
    cwd: str,
    status: str,
    agent: str | None = None,
    recent: bool = False,
) -> str:
    """Render a session's top-level header line.

    `recent=True` appends a trailing 🆕 marker to highlight that the bot
    has posted into this thread within the last N seconds. The leading
    🟢/⚪ status glyph stays constant so the only visible change in
    Element's thread list is the trailing emoji appearing/disappearing.
    """
    prefix = f"[{agent_label(agent)}] "
    if status == "active":
        suffix = "  🆕" if recent else ""
        return f"🟢 {prefix}{name}  ·  {cwd}{suffix}"
    return f"⚪ {prefix}{name}  ·  {cwd}  (ended)"


def _attach_body(text: str) -> str:
    """Return *text* attached to a prefix line, preserving markdown structure.

    Single-line text follows the prefix inline. Multi-line text is separated
    by a blank line so Element's markdown renderer treats the content as its
    own block — headings, bullet lists, fenced code, etc. in CC's reply
    render as themselves rather than getting flattened into one monospaced
    code block.
    """
    if "\n" in text:
        return f"\n\n{text}"
    return f" {text}"


def format_event(kind: str, data: dict[str, Any]) -> str:
    prefix = f"[{agent_label(str(data.get('agent', 'claude')))}] "
    if kind == "prompt":
        return f"{prefix}👤{_attach_body(str(data.get('text', '')))}"
    if kind == "response":
        body = f"{prefix}🤖{_attach_body(str(data.get('text', '')))}"
        summary = data.get("tool_summary")
        if summary:
            body += f"\n_↳ {summary}_"
        return body
    if kind == "notification":
        msg = str(data.get("message", "")) or "waiting for input"
        tool_request = str(data.get("tool_request", "")).strip()
        ctx = str(data.get("context", "")).strip()

        question = parse_ask_user_question(tool_request)
        if question is not None:
            parts = [f"{prefix}❓ {question['question']}"]
            for i, opt in enumerate(question["options"], start=1):
                if opt.get("description"):
                    parts.append(f"- **{i}.** {opt['label']} — _{opt['description']}_")
                else:
                    parts.append(f"- **{i}.** {opt['label']}")
            parts.append(
                "_Reply with the option number (e.g. `2`), "
                "or type free-form text to write a custom answer._"
            )
            if ctx:
                parts.append(f"```\n{ctx}\n```")
            return "\n".join(parts)

        parts = [f"{prefix}⏸ {msg}"]
        if tool_request:
            tr = (
                tool_request
                if len(tool_request) <= _TOOL_REQUEST_CAP
                else tool_request[:_TOOL_REQUEST_CAP] + "…"
            )
            parts.append(f"_Asking permission for:_ `{tr}`")
            parts.append("_Reply `1` to approve, `2` to deny, `3` to allow for session._")
        if ctx:
            parts.append(f"```\n{ctx}\n```")
        return "\n".join(parts)
    if kind == "error":
        return f"{prefix}❌ {data.get('text', '')}"
    return f"{prefix}[{kind}] {data!r}"


_ASK_USER_QUESTION_RE = re.compile(r"^AskUserQuestion\((\{.*\})\)\s*$", re.DOTALL)


def parse_ask_user_question(tool_request: str) -> dict[str, Any] | None:
    """If *tool_request* is CC's AskUserQuestion tool call, return the first
    pending question as ``{"question": str, "options": [{"label", "description"}]}``.

    Returns None for any other tool, malformed JSON, or empty questions list.
    AskUserQuestion can hold multiple questions; we surface the first one and
    rely on CC re-firing the notification hook for subsequent questions.
    """
    m = _ASK_USER_QUESTION_RE.match(tool_request.strip())
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    if not isinstance(first, dict):
        return None
    options_raw = first.get("options")
    if not isinstance(options_raw, list):
        return None
    options: list[dict[str, str]] = []
    for o in options_raw:
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if not label:
            continue
        options.append({"label": label, "description": str(o.get("description") or "").strip()})
    if not options:
        return None
    return {"question": str(first.get("question") or "").strip(), "options": options}


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
