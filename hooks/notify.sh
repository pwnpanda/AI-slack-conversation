#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
msg="$(printf '%s' "$input" | jq -r '.message // empty' 2>/dev/null || true)"
if [ -z "$sid" ]; then
  printf '{}\n'
  exit 0
fi
payload="$(jq -n --arg sid "$sid" --arg agent "$agent" --arg m "$msg" \
  '{v:1,kind:"notification",session_id:$sid,agent:$agent,message:$m}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
printf '{}\n'
exit 0
