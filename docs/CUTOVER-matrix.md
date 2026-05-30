# Matrix cutover — morning checklist

Branch `matrix-port` has the full port. Tests are green (verify below). The
Slack daemon is still running on `main` — nothing has been touched live.

## 1. Review the diff (5 min)

```bash
cd ~/git/priv/claude-slack-bot
git fetch  # not strictly needed, branch is local
git log main..matrix-port --oneline
git diff main..matrix-port --stat
git diff main..matrix-port -- src/slackbot/matrix_io.py
git diff main..matrix-port -- src/slackbot/__main__.py
git diff main..matrix-port -- src/slackbot/registry.py
```

Things to spot-check:
- `matrix_io.py` uses `room_send` with the right `m.relates_to.rel_type`
  values (`m.thread` for thread replies, `m.replace` for edits,
  `m.annotation` for reactions).
- `__main__.py` no longer imports `slack_*` anywhere, uses `sync_forever`,
  and still wires up `reader_pump`, `reaper`, `watchdog_heartbeat`.
- `registry.py` has the schema rename and the one-time "drop old DB"
  migration when it sees a `slack_channel` column.
- `pyproject.toml` no longer has `slack-sdk` / `slack-bolt`, has
  `matrix-nio>=0.25.2`.

## 2. Local sanity check

```bash
cd ~/git/priv/claude-slack-bot
git checkout matrix-port
uv sync
uv run pytest -q
```

Expect: all tests pass, no skipped Matrix tests.

## 3. Smoke test against the live homeserver (no cutover yet)

```bash
# Start the new daemon on a non-conflicting port so the Slack daemon keeps running:
SLACKBOT_PORT=8788 uv run python -m slackbot &
NEW_PID=$!
sleep 2

# Drive a fake session through the side daemon
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"start","session_id":"matrix-smoke","cwd":"/tmp",
       "zellij_session":"main","zellij_pane_id":"0","resumed":false,
       "agent":"claude"}' \
  http://127.0.0.1:8788/event
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"name","session_id":"matrix-smoke","name":"matrix-smoke"}' \
  http://127.0.0.1:8788/event
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"prompt","session_id":"matrix-smoke","text":"hello matrix"}' \
  http://127.0.0.1:8788/event
sleep 2

# Stop the side daemon (Slack daemon untouched)
kill $NEW_PID
```

Open Element X and confirm:
- A top-level message appears in the **Claude Code** room with the name `matrix-smoke`.
- A thread under it contains `👤 hello matrix`.

If both appear → cutover safe. If not → see "Rollback" below.

## 4. Cutover (the actual switch — ~30s downtime)

```bash
# Merge the port to main
cd ~/git/priv/claude-slack-bot
git checkout main
git merge --no-ff matrix-port -m "merge: port from Slack to Matrix"

# Restart the live daemon — it picks up the new code + the MATRIX_* env vars
# already present in ~/.config/claude-slack-bot/env
systemctl --user restart claude-slack-bot
sleep 3
systemctl --user is-active claude-slack-bot
journalctl --user -u claude-slack-bot --since '10 sec ago' --no-pager | tail -20
```

Look for:
- `slackbot.main starting claude-slack-bot port=8787` (or whatever you set)
- A `nio.client` connect line and a successful `/sync` cycle
- `re-attached N transcript readers on startup` (existing CC sessions reattach)
- No tracebacks

## 5. Verify end-to-end

In a CC session (any pane):
- Type a prompt. Watch Element X → it should mirror as a threaded message
  within ~1s.
- Reply to that thread from Element X on your phone. The reply text should
  type into the CC pane.
- Check that the ✅ reaction appears on your reply once delivered.

## 6. Clean up env file (optional, after a stable day)

```bash
# Remove the now-unused Slack vars
sed -i '/^SLACK_/d' ~/.config/claude-slack-bot/env
```

## Rollback (if cutover step 4 or 5 fails)

```bash
cd ~/git/priv/claude-slack-bot
git checkout main
git reset --hard HEAD~1   # undo the merge commit
systemctl --user restart claude-slack-bot
```

Bot is back on Slack. Investigate the failure on `matrix-port` (still
available in git), fix, re-attempt.

## Known caveats

- **Element X push notification latency**: depends on the phone's battery
  optimisation settings for both ntfy and Element X. dontkillmyapp.com has
  per-device guidance.
- **DNS negative caching** for `chat.robinlunde.com`: resolved on cellular
  but may still NXDOMAIN on the LAN for up to 1 hour after the record was
  first added.
- **First Element X login**: when you log into `@robin:chat.robinlunde.com`,
  Element X will ask for a "session backup" / "device verification" — there
  are no other devices to verify against and no E2EE rooms, so skip these.
