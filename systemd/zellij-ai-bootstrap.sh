#!/usr/bin/env bash
# Ensure a detached zellij session named "ai" exists, for claude-slack-bot
# agent panes. Invoked as the ExecStart of zellij-ai.service.
#
# `zellij attach --create-background` creates a detached (headless) session
# when none exists and is a no-op otherwise — it never spawns a duplicate.
# Its one quirk: it exits non-zero with "Session already exists" when the
# session is present, so we treat any non-zero result as success only after
# confirming the session is actually up via list-sessions.
set -euo pipefail

SESSION="ai"

if zellij attach --create-background "$SESSION" 2>/dev/null; then
    echo "created detached zellij session '$SESSION'"
    exit 0
fi

if zellij list-sessions --short --no-formatting 2>/dev/null | grep -qx "$SESSION"; then
    echo "zellij session '$SESSION' already running; nothing to do"
    exit 0
fi

echo "failed to create zellij session '$SESSION'" >&2
exit 1
