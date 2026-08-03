#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$input" | jq -r '.cwd // env.GEMINI_CWD // empty' 2>/dev/null || true)"
if [ -z "$sid" ]; then
  printf '{}\n'
  exit 0
fi
resumed_flag=false
source="$(printf '%s' "$input" | jq -r '.source // empty' 2>/dev/null || true)"
case "${CLAUDE_HOOK_SOURCE:-}${source}" in
  resume|*resume*) resumed_flag=true ;;
esac
# $PPID is the parent process — the CC/Codex/Gemini process that invoked this hook.
# We record it so the daemon can probe liveness with kill -0 instead of falling back
# to coarse time-based "looks dead" heuristics.
transcript_path="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
cc_pid="$PPID"
payload="$(jq -n \
  --arg sid "$sid" \
  --arg agent "$agent" \
  --arg cwd "${cwd:-$PWD}" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" \
  --arg zp "${ZELLIJ_PANE_ID:-}" \
  --arg tx "$transcript_path" \
  --argjson resumed "$resumed_flag" \
  --argjson cc_pid "$cc_pid" \
  '{v:1,kind:"start",session_id:$sid,agent:$agent,cwd:$cwd,zellij_session:$zs,
    zellij_pane_id:$zp,transcript_path:$tx,resumed:$resumed,cc_pid:$cc_pid}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
printf '{}\n'
exit 0
