#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

python3 "${SOURCE_DIR}/install_agent_hooks.py" \
  --agent gemini \
  --settings "${HOME}/.gemini/settings.json" \
  --hooks-dir "${HOME}/.gemini/hooks/claude-slack-bot" \
  SessionStart:session_start.sh \
  BeforeAgent:prompt.sh \
  AfterAgent:stop.sh \
  Notification:notify.sh \
  SessionEnd:session_end.sh
