#!/usr/bin/env bash
set -uo pipefail
PORT="${SLACKBOT_PORT:-8787}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -z "$sid" ] && exit 0
resumed_flag=false
case "${CLAUDE_HOOK_SOURCE:-}" in
  resume|*resume*) resumed_flag=true ;;
esac
payload="$(jq -n \
  --arg sid "$sid" \
  --arg cwd "${cwd:-$PWD}" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" \
  --arg zp "${ZELLIJ_PANE_ID:-}" \
  --argjson resumed "$resumed_flag" \
  '{v:1,kind:"start",session_id:$sid,cwd:$cwd,zellij_session:$zs,zellij_pane_id:$zp,resumed:$resumed}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
exit 0
