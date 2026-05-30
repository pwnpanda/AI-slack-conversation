# claude-slack-bot

Bridge between Claude Code, Codex, and Gemini CLI sessions and Matrix: mirrors selected agent sessions into Matrix threads, and types your Matrix replies back into the originating Zellij pane.

> Migrated from Slack to Matrix on the `matrix-port` branch. The repo name (`claude-slack-bot`) and Python package (`slackbot`) are unchanged from the Slack era; the transport underneath them is now Matrix.

## What this does

Registers Claude sessions with `/rn <name>`, and auto-registers Codex/Gemini sessions because those CLIs do not provide the same custom slash-command path. The bot posts a top-level message naming the session and mirrors every subsequent prompt, response, and notification into the thread beneath it. Replying in that Matrix thread types your reply into the originating Zellij pane (via `zellij action write-chars`).

## Use cases

- Mobile notifications when CC asks for input or finishes a long-running turn
- Reply to CC from your phone — text is typed into the live terminal session
- Monitor multiple parallel CC sessions from one Matrix room
- Long-lived searchable archive of selected bug-bounty sessions

## Requirements

- Linux or WSL2 (with systemd enabled in `/etc/wsl.conf`)
- Python 3.13, `uv`
- Zellij ≥ 0.44
- `jq`, `curl` (for hook scripts)
- A self-hosted Matrix homeserver (Continuwuity recommended; Synapse works too) reachable from the daemon host
- A Matrix bot account with an access token
- Claude Code CLI, Codex CLI, or Gemini CLI

## Installation / Setup

### 1. Provision the Matrix bot account + rooms

Server-side provisioning lives in `homelabs/Matrix/provision-users.sh` — run that on the Continuwuity LXC to create the `@ai-bot:chat.robinlunde.com` user, the human user, and the three per-agent rooms (`#claude`, `#codex`, `#gemini`). The script prints the bot's access token at the end; copy it into the env file in step 2.

If you are setting this up against a different homeserver, the steps are:

1. Register the bot user via your server's admin API (`POST /_synapse/admin/v2/users/...` for Synapse, equivalent admin command room on Continuwuity).
2. Log in once as the bot via `POST /_matrix/client/v3/login` with `type=m.login.password` and persist the returned `access_token` + `device_id`.
3. Create one room per agent (`#claude`, `#codex`, `#gemini`), invite the bot and your user, accept invites.
4. Record each room's internal ID (`!abc:server`) — that is what the daemon needs, not the human-readable alias.

### 2. Create the env file

```bash
mkdir -p ~/.config/claude-slack-bot
cat > ~/.config/claude-slack-bot/env <<'EOF'
MATRIX_HOMESERVER=https://chat.robinlunde.com
MATRIX_USER_ID=@ai-bot:chat.robinlunde.com
MATRIX_ACCESS_TOKEN=syt_...
MATRIX_DEVICE_ID=slackbot-daemon
# Fallback room used when an agent has no per-agent room set.
MATRIX_ROOM_ID=!default:chat.robinlunde.com
# Per-agent rooms.
MATRIX_ROOM_ID_CLAUDE=!claude:chat.robinlunde.com
MATRIX_ROOM_ID_CODEX=!codex:chat.robinlunde.com
MATRIX_ROOM_ID_GEMINI=!gemini:chat.robinlunde.com
SLACKBOT_PORT=8787
LOG_LEVEL=INFO
EOF
chmod 600 ~/.config/claude-slack-bot/env
```

### 3. Install the daemon

```bash
cd ~/git/priv/claude-slack-bot
uv venv && uv sync
mkdir -p ~/.config/systemd/user
cp systemd/claude-slack-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-slack-bot
systemctl --user status claude-slack-bot
```

Expected status: `active (running)`.

### 4. Install the agent hooks

```bash
bash ~/git/priv/claude-slack-bot/hooks/install.sh
bash ~/git/priv/claude-slack-bot/hooks/install-codex.sh
bash ~/git/priv/claude-slack-bot/hooks/install-gemini.sh
```

This installs the bridge hooks for Claude, Codex, and Gemini:

- Claude: `~/.claude/settings.json`
- Codex: `~/.codex/hooks.json`
- Gemini: `~/.gemini/settings.json`

All three post the same event contract to the daemon and include an agent label.

### 5. Phone client

Install **Element X** on Android (or iOS). Log in as your human user against `chat.robinlunde.com`. On Android, install the **ntfy** F-Droid app first and point it at `https://push.robinlunde.com` so Element X picks ntfy as its UnifiedPush distributor on first login (no Google FCM needed). See `docs/plan-matrix-migration.md` §4 for the full push wiring.

## Usage

1. Start your agent inside a Zellij pane
2. Claude: run `/rn my-session`; Codex/Gemini: the thread appears automatically
3. Codex: optionally rename the thread with `rn my-session`; the hook blocks that control prompt from reaching the model
4. Type prompts in the agent; they mirror into the Matrix thread
5. From Element X (mobile or desktop), reply in the thread — the text is typed into the agent pane (the pane briefly takes focus)

## Testing

```bash
cd ~/git/priv/claude-slack-bot
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
shellcheck hooks/*.sh
```

Manual smoke test (with daemon running):

```bash
# 1. fake a session_start
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"start","session_id":"smoke","cwd":"/tmp",
       "zellij_session":"main","zellij_pane_id":"0","resumed":false}' \
  http://127.0.0.1:8787/event

# 2. name it (this triggers the Matrix post)
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"name","session_id":"smoke","name":"smoke-test"}' \
  http://127.0.0.1:8787/event

# 3. simulate a prompt
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"prompt","session_id":"smoke","text":"hello"}' \
  http://127.0.0.1:8787/event

# 4. end the session
curl -s -H 'content-type: application/json' \
  -d '{"v":1,"kind":"end","session_id":"smoke","reason":"done"}' \
  http://127.0.0.1:8787/event
```

Then in Element X: reply to the thread; verify text appears typed into Zellij pane 0 (`zellij --session main attach` to view). Pane briefly steals focus — accepted cost.

## Deployment

`systemd --user` unit (`systemd/claude-slack-bot.service`). Restart-on-failure baked in. Logs:

```bash
journalctl --user -u claude-slack-bot -f
```

## Implemented features

- HTTP event endpoint at `http://127.0.0.1:8787/event`
- SQLite-backed session/event registry
- Matrix listener via `matrix-nio` `sync_forever` (no inbound port required)
- Top-level message per named session, thread per session lifetime (`m.thread` relations)
- Codex/Gemini auto-registration with names like `codex-project-abcdef12`
- Portable Codex rename prompt: `rn my-session`
- Agent labels in Matrix messages: `[Claude]`, `[Codex]`, `[Gemini]`
- Optional per-agent Matrix rooms via `MATRIX_ROOM_ID_CLAUDE`, `MATRIX_ROOM_ID_CODEX`, `MATRIX_ROOM_ID_GEMINI`
- Event buffering for prompts that arrive before naming
- Resume detection: same `session_id` → flips top-level back to 🟢
- Name reclaim: new session adopting an existing name reuses the same thread
- Reply routing: thread reply → `zellij action write-chars` into the originating pane
- ✅/⚠️/🚫 Unicode reactions confirming delivery state
- `/new <name>` top-level command spawns a new CC pane and types `/rn <name>` into it
- Idempotent hook installer

## Planned features

- Multi-session-per-project disambiguation (currently 1:1 enforced)
- Matrix slash commands beyond `/new` (`/cc-list`, `/cc-mute`, `/cc-status`)
- Verbose mode posting individual tool calls
- Truncation-with-link for very long responses
- **Dedicated `ai` zellij session for agent panes.** Today `/new` spawns
  into the same zellij session the human is using, which steals focus.
  Move all agent panes into a separate `ai` session (the `ZellijActuator`
  already takes a session name per call). The `/new` command would spawn
  a new pane/tab in `ai` instead of `main`; the human's `main` session is
  never touched. Configurable via `SLACKBOT_NEW_PANE_ZELLIJ_SESSION`
  already, but a one-time bootstrap (create `ai` session if missing,
  attach the bot to it) would polish it.
- **Multi-stage answer support.** CC's `AskUserQuestion` is multi-stage:
  pick an option, then provide a follow-up answer in a subsequent prompt.
  The bot currently treats every Matrix reply as a single typed string into
  the pane, so a user replying with the full answer in one go can miss the
  intermediate "pick an option" step. Tracking the pending question state
  per session and routing replies through stages is open work.
- Optional E2EE (requires `matrix-nio[e2e]` + libolm + per-device key
  management). Out for v1 because the homeserver is on the LAN.

## Worker model

The daemon uses a worker-per-conversation model. Each `cc_session_id` owns
an asyncio Task + Queue + per-worker uuid-dedup set. A `TranscriptReader`
tails the JSONL file CC writes; new user/assistant messages flow into the
worker, which decides whether to mirror them to Matrix (skipping uuids
already posted, suppressing echoes from Matrix-driven deliveries).

A `ReplyRouter` enqueues incoming Matrix thread replies into the matching
worker. The global `ZellijActuator` still owns a single `asyncio.Lock` so the
three-call `focus → write-chars → Enter` sequence is atomic across workers.

`session_is_alive` is the only liveness gate. It scans `/proc/*/cmdline`
for the session id (exact argv token) or `--resume <name>` (adjacent argv
tokens), with a 10s TTL cache and the scan offloaded to `asyncio.to_thread`.
The registry's `status` column is diagnostic only.

systemd watchdog: the unit is `Type=notify`; daemon calls `sd_notify`
on startup (`READY=1`), every 60s, and on every received Matrix sync
response. `WatchdogSec=600` so a hung daemon gets restarted within 10
minutes.

After upgrading, restart your CC sessions once so the new `session_start.sh`
runs and writes `transcript_path` into the registry. Existing rows without
`transcript_path` keep working (the transcript reader isn't attached, but
Matrix replies still deliver and notifications still mirror).

## Migrated from Slack (2026-05-29)

This bot started life on Slack Socket Mode. After repeated silent-event-drop
incidents (`slack-sdk` issue [#1379](https://github.com/slackapi/python-slack-sdk/issues/1379)
and friends) the transport was swapped for self-hosted Matrix per
`docs/plan-matrix-migration.md`. The Slack-specific modules (`slack_io.py`,
`slack_poller.py`, `slack_commands.py`) were deleted per the
"replace, don't deprecate" policy; the worker model, transcript reader,
registry, liveness check, and Zellij actuator carried over unchanged.

The registry schema renamed `slack_channel` → `matrix_room_id` and
`slack_thread_ts` → `matrix_thread_root`. On first start the new daemon
detects a legacy DB and drops it, logging a warning — old Slack thread
IDs would be meaningless under Matrix anyway. There is no two-way
backwards-compat shim; if you ever need the Slack version back, check out
the last pre-migration commit on `main`.

## Claude Sessions

| Session | Summary | Date |
|---------|---------|------|
| `obsidian-publish-rule` | Added a global `ExitPlanMode` hook that publishes plans to Obsidian `for_evaluation/<project>/` for mobile review, plus `reconcile_plans.py` for manual copy-back, and a CLAUDE.md rule. Source lives in `homelabs/Obsidian/plan-review/`, symlinked into `~/.claude/hooks/`. | 2026-05-30 |
| `matrix-port` | Ported the daemon from Slack to Matrix (matrix-nio): replaced `slack_io`/`slack_poller`/`slack_commands`, renamed registry schema, rewrote `__main__` around `sync_forever`. | 2026-05-29 |
| `debug-gemini-routing` | Fixed `rn` blocking for Gemini and verified Slack reply routing via trace logs. | 2026-05-25 |
| `fix-rn-blocking` | Fixed `rn` command not blocking for Gemini; added debug logging to reply router. | 2026-05-25 |
| `run-testing` | Ran full test suite to verify project health; all 57 tests passed. | 2026-05-25 |
| `test-conversation` | Confirmed the Codex session wiring and explained the rename hook block message. | 2026-05-25 |
| `codex-auto-register` | Added Codex/Gemini auto-registration and a portable `rn name` rename prompt. | 2026-05-25 |
| `agent-prefix-routing` | Added Claude/Codex/Gemini labels, optional per-agent channels, and installers for all three agents. | 2026-05-25 |
| `repo-feature-implementation` | Assessed Codex compatibility for the existing Slack/Zellij hook bridge. | 2026-05-25 |
| `slackbot-claude` | Brainstormed, wrote design spec, wrote 15-task plan, implemented all tasks | 2026-05-24 |
