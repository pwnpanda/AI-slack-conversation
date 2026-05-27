#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
reason="$(printf '%s' "$input" | jq -r '.reason // "unknown"' 2>/dev/null || true)"
if [ -z "$sid" ]; then
  printf '{}\n'
  exit 0
fi
cc_pid="$PPID"
payload="$(jq -n \
  --arg sid "$sid" --arg agent "$agent" --arg r "$reason" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" --arg zp "${ZELLIJ_PANE_ID:-}" \
  --argjson cc_pid "$cc_pid" \
  '{v:1,kind:"end",session_id:$sid,agent:$agent,reason:$r,
    zellij_session:$zs,zellij_pane_id:$zp,cc_pid:$cc_pid}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
printf '{}\n'
exit 0
