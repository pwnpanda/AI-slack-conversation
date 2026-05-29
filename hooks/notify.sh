#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
msg="$(printf '%s' "$input" | jq -r '.message // empty' 2>/dev/null || true)"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"

if [ -z "$sid" ]; then
  printf '{}\n'
  exit 0
fi

# Only fire on actual permission asks. Plain "Claude is waiting for your
# input" notifications are noise in bypass-permissions mode, so the hook
# drops them entirely instead of POSTing.
case "$(printf '%s' "$msg" | tr '[:upper:]' '[:lower:]')" in
  *permission*|*approve*|*allow*|*needs\ your*) ;;
  *)
    printf '{}\n'
    exit 0
    ;;
esac

# Enrich with context: last assistant text + the most-recent tool_use, so
# the Slack reader can answer with the option number.
context=""
tool_request=""
if [ -n "$transcript" ] && [ -r "$transcript" ]; then
  sleep 0.2
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    t="$(printf '%s' "$line" | jq -r '
      select(.message.role == "assistant") | .message.content
      | if type == "string" then .
        elif type == "array" then (map(select(.type == "text") | .text) | join("\n"))
        else empty end
    ' 2>/dev/null || true)"
    if [ -n "$t" ]; then
      context="$t"
      break
    fi
  done < <(tac "$transcript" 2>/dev/null)

  tool_request="$(tac "$transcript" 2>/dev/null \
    | jq -r 'select(.message.role == "assistant") | .message.content
              | if type == "array" then
                  (map(select(.type == "tool_use")) | last
                    | if . then "\(.name)(\(.input | tojson))" else empty end)
                else empty end' 2>/dev/null \
    | awk 'NF { print; exit }' || true)"
fi

cc_pid="$PPID"
payload="$(jq -n \
  --arg sid "$sid" --arg agent "$agent" --arg m "$msg" \
  --arg ctx "$context" --arg tool "$tool_request" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" --arg zp "${ZELLIJ_PANE_ID:-}" \
  --argjson cc_pid "$cc_pid" \
  '{v:1,kind:"notification",session_id:$sid,agent:$agent,message:$m,
     context:$ctx,tool_request:$tool,
     zellij_session:$zs,zellij_pane_id:$zp,cc_pid:$cc_pid}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
printf '{}\n'
exit 0
