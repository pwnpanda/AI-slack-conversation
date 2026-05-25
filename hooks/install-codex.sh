#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

python3 "${SOURCE_DIR}/install_agent_hooks.py" \
  --agent codex \
  --settings "${HOME}/.codex/hooks.json" \
  --hooks-dir "${HOME}/.codex/hooks/claude-slack-bot" \
  SessionStart:session_start.sh \
  UserPromptSubmit:prompt.sh \
  Stop:stop.sh
