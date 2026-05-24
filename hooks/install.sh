#!/usr/bin/env bash
set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks/claude-slack-bot"
SETTINGS="${CLAUDE_DIR}/settings.json"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "${HOOKS_DIR}"
for f in session_start.sh prompt.sh stop.sh notify.sh session_end.sh; do
  install -m 0755 "${SOURCE_DIR}/${f}" "${HOOKS_DIR}/${f}"
done

new_hooks="$(jq -n --arg dir "${HOOKS_DIR}" '{
  SessionStart: [{hooks: [{type: "command", command: ($dir + "/session_start.sh")}]}],
  UserPromptSubmit: [{hooks: [{type: "command", command: ($dir + "/prompt.sh")}]}],
  Stop: [{hooks: [{type: "command", command: ($dir + "/stop.sh")}]}],
  Notification: [{hooks: [{type: "command", command: ($dir + "/notify.sh")}]}],
  SessionEnd: [{hooks: [{type: "command", command: ($dir + "/session_end.sh")}]}]
}')"

if [ -f "${SETTINGS}" ]; then
  existing="$(cat "${SETTINGS}")"
else
  existing="{}"
fi

merged="$(jq --argjson new "${new_hooks}" '.hooks = (.hooks // {}) * $new' <<<"${existing}")"

tmp="$(mktemp "${SETTINGS}.XXXXXX")"
printf '%s\n' "${merged}" >"${tmp}"
mv "${tmp}" "${SETTINGS}"

echo "Installed hooks into ${HOOKS_DIR}"
echo "Updated ${SETTINGS}"
