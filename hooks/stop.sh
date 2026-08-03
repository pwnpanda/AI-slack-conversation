#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
if [ -z "$sid" ]; then
  printf '{}\n'
  exit 0
fi

# Codex and Gemini expose the response directly. Claude falls back to transcript parsing.
last_text="$(printf '%s' "$input" | jq -r '.last_assistant_message // .prompt_response // empty' 2>/dev/null || true)"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
if [ -z "$last_text" ] && [ -n "$transcript" ] && [ -r "$transcript" ]; then
  # CC may fire Stop before flushing the latest assistant message to disk.
  # A brief sleep mitigates the race; we then re-read.
  sleep 0.4
  # Read transcript reversed line-by-line; first non-empty assistant text wins.
  # content may be a string (older format) or an array of blocks (current).
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    txt="$(printf '%s' "$line" | jq -r '
      select(.message.role == "assistant") | .message.content
      | if type == "string" then .
        elif type == "array" then (map(select(.type == "text") | .text) | join("\n"))
        else empty end
    ' 2>/dev/null || true)"
    if [ -n "$txt" ]; then
      last_text="$txt"
      break
    fi
  done < <(tac "$transcript" 2>/dev/null)
fi

cc_pid="$PPID"
payload="$(jq -n \
  --arg sid "$sid" --arg agent "$agent" --arg t "$last_text" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" --arg zp "${ZELLIJ_PANE_ID:-}" \
  --arg tx "$transcript" \
  --argjson cc_pid "$cc_pid" \
  '{v:1,kind:"response",session_id:$sid,agent:$agent,text:$t,
    zellij_session:$zs,zellij_pane_id:$zp,transcript_path:$tx,cc_pid:$cc_pid}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
printf '{}\n'
exit 0
