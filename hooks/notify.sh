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

# Enrich the notification with context: last assistant text + the tool_use that
# is most likely the reason CC is asking for input. Lets the Slack reader see
# what's being asked and reply with the option number.
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

payload="$(jq -n --arg sid "$sid" --arg agent "$agent" --arg m "$msg" \
  --arg ctx "$context" --arg tool "$tool_request" \
  '{v:1,kind:"notification",session_id:$sid,agent:$agent,message:$m,
     context:$ctx,tool_request:$tool}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
  -d "$payload" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1 || true
printf '{}\n'
exit 0
