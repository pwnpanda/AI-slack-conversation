#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0

# Best-effort: pull last assistant message from transcript_path if available
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
last_text=""
if [ -n "$transcript" ] && [ -r "$transcript" ]; then
  last_text="$(tac "$transcript" 2>/dev/null \
    | jq -r 'select(.message.role=="assistant") | .message.content // empty' 2>/dev/null \
    | grep -v '^$' \
    | head -n 1 || true)"
fi

payload="$(jq -n --arg sid "$sid" --arg t "$last_text" \
  '{v:1,kind:"response",session_id:$sid,text:$t}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
