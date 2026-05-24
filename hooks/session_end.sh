#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
reason="$(printf '%s' "$input" | jq -r '.reason // "unknown"' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0
payload="$(jq -n --arg sid "$sid" --arg r "$reason" \
  '{v:1,kind:"end",session_id:$sid,reason:$r}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
