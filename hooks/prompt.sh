#!/usr/bin/env bash
set -euo pipefail
PORT="${SLACKBOT_PORT:-8787}"
agent="${SLACKBOT_AGENT:-claude}"
input="$(cat || true)"
sid="$(printf '%s' "$input" | jq -r '.session_id // env.GEMINI_SESSION_ID // empty' 2>/dev/null || true)"
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null || true)"
# CC includes cwd in the UserPromptSubmit JSON; fall back to $PWD so the
# bot can backfill the registry when the row was auto-created without it.
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -n "$cwd" ] || cwd="$PWD"
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
  cc_pid="$PPID"
  name_payload="$(jq -n \
    --arg sid "$sid" --arg agent "$agent" --arg n "$name" --arg cwd "$cwd" \
    --arg zs "${ZELLIJ_SESSION_NAME:-}" --arg zp "${ZELLIJ_PANE_ID:-}" \
    --argjson cc_pid "$cc_pid" \
    '{v:1,kind:"name",session_id:$sid,agent:$agent,name:$n,cwd:$cwd,
      zellij_session:$zs,zellij_pane_id:$zp,cc_pid:$cc_pid}')"
  if [ "$agent" = "codex" ] || [ "$agent" = "gemini" ]; then
    if post "$name_payload"; then
      reason="Renamed Matrix thread to $name"
    else
      reason="Matrix bridge unavailable; rename command not sent to $agent"
    fi
    printf '{}\n'
    printf '%s\n' "$reason" >&2
    exit 2
  fi
fi

cc_pid="$PPID"
prompt_payload="$(jq -n \
  --arg sid "$sid" --arg agent "$agent" --arg t "$prompt" --arg cwd "$cwd" \
  --arg zs "${ZELLIJ_SESSION_NAME:-}" --arg zp "${ZELLIJ_PANE_ID:-}" \
  --argjson cc_pid "$cc_pid" \
  '{v:1,kind:"prompt",session_id:$sid,agent:$agent,text:$t,cwd:$cwd,
    zellij_session:$zs,zellij_pane_id:$zp,cc_pid:$cc_pid}')"
post "$prompt_payload" || true

if [ -n "$name" ]; then
  post "$name_payload" || true
fi
printf '{}\n'
exit 0
