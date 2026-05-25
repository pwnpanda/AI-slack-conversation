#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
printf "DEBUG: agent=%s sid=%s\n" "$agent" "$sid" >> /tmp/slackbot-hooks.log
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null || true)"
if [ -z "$sid" ]; then
  printf '{}\n'
  exit 0
fi

post() {
  curl -fsS --max-time 1 -H 'content-type: application/json' \
    -d "$1" "http://127.0.0.1:${PORT}/event" >/dev/null 2>&1
}

# Detect rename commands and emit a name event. Codex does not support custom
# slash commands, so bare "rn name" is the portable form.
name="$(printf '%s' "$prompt" \
  | sed -n -E \
    -e 's@^rn[[:space:]]+([^[:space:]]+)[[:space:]]*$@\1@p' \
    -e 's@^[/#!](rn|rename|register)[[:space:]]+([^[:space:]]+)[[:space:]]*$@\2@p')"
if [ -n "$name" ]; then
  name_payload="$(jq -n --arg sid "$sid" --arg agent "$agent" --arg n "$name" \
    '{v:1,kind:"name",session_id:$sid,agent:$agent,name:$n}')"
  if [ "$agent" = "codex" ] || [ "$agent" = "gemini" ]; then
    if post "$name_payload"; then
      reason="Renamed Slack thread to $name"
    else
      reason="Slackbot unavailable; rename command not sent to $agent"
    fi
    printf '{}\n'
    printf '%s\n' "$reason" >&2
    exit 2
  fi
fi

prompt_payload="$(jq -n --arg sid "$sid" --arg agent "$agent" --arg t "$prompt" \
  '{v:1,kind:"prompt",session_id:$sid,agent:$agent,text:$t}')"
post "$prompt_payload" || true

if [ -n "$name" ]; then
  post "$name_payload" || true
fi
printf '{}\n'
exit 0
