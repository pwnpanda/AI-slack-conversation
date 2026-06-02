#!/usr/bin/env bash
# Bring up the 'ai' zellij session if (and only if) it isn't already
# running. Invoked as the ExecStart of zellij-ai.service.
#
# Why a wrapper: a bare `zellij --session ai` ExecStart would attach a
# second client to an existing session (under script(1)'s pty wrapper),
# which is harmless but pollutes the session with a never-typing client
# and double-runs zellij. The pre-check keeps the unit idempotent on
# restart, even when the user (or some other path) already spun 'ai' up.
set -euo pipefail

SESSION="ai"

if zellij list-sessions --short --no-formatting 2>/dev/null | grep -qx "$SESSION"; then
    echo "zellij session '$SESSION' already running; nothing to do"
    exit 0
fi

echo "spawning zellij session '$SESSION'"
# `script` provides the pty zellij needs; setsid detaches from the systemd
# control terminal so zellij keeps running after this wrapper returns.
setsid /usr/bin/script -qec "/usr/bin/zellij --session $SESSION" /dev/null \
    < /dev/null > /dev/null 2>&1 &
disown

# Give zellij a moment to register its socket, then verify.
for _ in 1 2 3 4 5 6 7 8; do
    if zellij list-sessions --short --no-formatting 2>/dev/null | grep -qx "$SESSION"; then
        echo "session '$SESSION' is up"
        exit 0
    fi
    sleep 0.5
done
echo "failed to bring up zellij session '$SESSION' within 4s" >&2
exit 1
