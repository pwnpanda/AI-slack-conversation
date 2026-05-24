#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0

post() {
  curl -fsS --max-time 1 -H 'content-type: application/json' \
    -d "$1" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
}

prompt_payload="$(jq -n --arg sid "$sid" --arg t "$prompt" \
  '{v:1,kind:"prompt",session_id:$sid,text:$t}')"
post "$prompt_payload"

# Detect /rn-style commands and emit a name event
name="$(printf '%s' "$prompt" \
  | sed -n -E 's@^/(rn|rename|register)[[:space:]]+([^[:space:]]+)[[:space:]]*$@\2@p')"
if [ -n "$name" ]; then
  name_payload="$(jq -n --arg sid "$sid" --arg n "$name" \
    '{v:1,kind:"name",session_id:$sid,name:$n}')"
  post "$name_payload"
fi
exit 0
