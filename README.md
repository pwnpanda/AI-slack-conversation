# claude-slack-bot

Bridge between Claude Code, Codex, and Gemini CLI sessions and Slack: mirrors selected agent sessions into Slack threads, and types your Slack replies back into the originating Zellij pane.

## What this does

Registers Claude sessions with `/rn <name>`, and auto-registers Codex/Gemini sessions because those CLIs do not provide the same custom slash-command path. The bot posts a top-level message naming the session and mirrors every subsequent prompt, response, and notification into the thread beneath it. Replying in that Slack thread types your reply into the originating Zellij pane (via `zellij action write-chars`).

## Use cases

- Mobile notifications when CC asks for input or finishes a long-running turn
- Reply to CC from your phone — text is typed into the live terminal session
- Monitor multiple parallel CC sessions from one Slack channel
- Long-lived searchable archive of selected bug-bounty sessions

## Requirements

- Linux or WSL2 (with systemd enabled in `/etc/wsl.conf`)
- Python 3.13, `uv`
- Zellij ≥ 0.44
- `jq`, `curl` (for hook scripts)
- A Slack workspace where you can create an app
- Claude Code CLI, Codex CLI, or Gemini CLI

## Installation / Setup

### 1. Create the Slack app

1. Visit https://api.slack.com/apps → "Create New App" → "From scratch"
2. Name: `claude-slack-bot`; pick your workspace
3. **Socket Mode** → enable, generate app-level token (`xapp-...`)
4. **OAuth & Permissions** → add bot scopes:
   `chat:write`, `chat:write.public`, `channels:history`, `groups:history`,
   `reactions:write`, `commands`, `app_mentions:read`
5. **Event Subscriptions** → enable, subscribe to bot events: `message.channels`, `message.groups`, `app_mention`
6. Install to workspace; copy `xoxb-...` bot token
7. Invite the bot to the channel you'll use (e.g. `/invite @claude-slack-bot`)
8. Right-click the channel → "Copy link" — the trailing path is the channel ID (`C0123…`)

### 2. Create the env file

```bash
mkdir -p ~/.config/claude-slack-bot
cat > ~/.config/claude-slack-bot/env <<'EOF'
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
SLACK_CHANNEL_ID=C…
# Optional: route agents to separate Slack channels. Defaults to SLACK_CHANNEL_ID.
# SLACK_CHANNEL_ID_CLAUDE=C…
# SLACK_CHANNEL_ID_CODEX=C…
# SLACK_CHANNEL_ID_GEMINI=C…
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

## Usage

1. Start your agent inside a Zellij pane
2. Claude: run `/rn my-session`; Codex/Gemini: the thread appears automatically
3. Codex: optionally rename the thread with `rn my-session`; the hook blocks that control prompt from reaching the model
4. Type prompts in the agent; they mirror into the Slack thread
5. From Slack (mobile or desktop), reply in the thread — the text is typed into the agent pane (the pane briefly takes focus)

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

# 2. name it (this triggers the Slack post)
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

Then in Slack: reply to the thread; verify text appears typed into Zellij pane 0 (`zellij --session main attach` to view). Pane briefly steals focus — accepted cost.

## Deployment

`systemd --user` unit (`systemd/claude-slack-bot.service`). Restart-on-failure baked in. Logs:

```bash
journalctl --user -u claude-slack-bot -f
```

## Implemented features

- HTTP event endpoint at `http://127.0.0.1:8787/event`
- SQLite-backed session/event registry
- Slack Socket Mode listener (no inbound port required)
- Top-level message per named session, thread per session lifetime
- Codex/Gemini auto-registration with names like `codex-project-abcdef12`
- Portable Codex rename prompt: `rn my-session`
- Agent labels in Slack messages: `[Claude]`, `[Codex]`, `[Gemini]`
- Optional per-agent Slack channels via `SLACK_CHANNEL_ID_CLAUDE`, `SLACK_CHANNEL_ID_CODEX`, `SLACK_CHANNEL_ID_GEMINI`
- Event buffering for prompts that arrive before naming
- Resume detection: same `session_id` → flips top-level back to 🟢
- Name reclaim: new session adopting an existing name reuses the same thread
- Reply routing: thread reply → `zellij action write-chars` into the originating pane
- ✅/⚠️/🚫 emoji reactions confirming delivery state
- Idempotent hook installer

## Planned features

- Multi-session-per-project disambiguation (currently 1:1 enforced)
- `/cc-list`, `/cc-mute`, `/cc-status` slash commands
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
  The bot currently treats every Slack reply as a single typed string into
  the pane, so a user replying with the full answer in one go can miss the
  intermediate "pick an option" step. Tracking the pending question state
  per session and routing replies through stages is open work.

## Worker redesign (2026-05-27)

The daemon now uses a worker-per-conversation model. Each `cc_session_id` owns
an asyncio Task + Queue + per-worker uuid-dedup set. A `TranscriptReader`
tails the JSONL file CC writes; new user/assistant messages flow into the
worker, which decides whether to mirror them to Slack (skipping uuids
already posted, suppressing echoes from Slack-driven deliveries).

A `ReplyRouter` enqueues incoming Slack thread replies into the matching
worker. The global `ZellijActuator` still owns a single `asyncio.Lock` so the
three-call `focus → write-chars → Enter` sequence is atomic across workers.

`session_is_alive` is the only liveness gate. It scans `/proc/*/cmdline`
for the session id (exact argv token) or `--resume <name>` (adjacent argv
tokens), with a 10s TTL cache and the scan offloaded to `asyncio.to_thread`.
The registry's `status` column is diagnostic only.

systemd watchdog: the unit is `Type=notify`; daemon calls `sd_notify`
on startup (`READY=1`), every 60s, and on every received Slack event.
`WatchdogSec=600` so a hung daemon gets restarted within 10 minutes.

After upgrading, restart your CC sessions once so the new `session_start.sh`
runs and writes `transcript_path` into the registry. Existing rows without
`transcript_path` keep working (the transcript reader isn't attached, but
Slack replies still deliver and notifications still mirror).

## Known Slack Socket Mode silent failure + our mitigation (2026-05-28)

The slack-sdk Socket Mode client has a long-standing, well-documented bug: the
WebSocket appears healthy (`is_connected()=True`, ping/pong flowing) but
incoming events stop being dispatched to handlers. The user message is in the
Slack channel; the bot never sees it.

Root cause (per [slack-sdk #1379](https://github.com/slackapi/python-slack-sdk/issues/1379)):
`SocketModeClient.is_connected()` returns True whenever `current_session` is
not None, without verifying the underlying socket is viable. The auto-reconnect
logic therefore never trips.

Reported repeatedly upstream — see [bolt-python #952](https://github.com/slackapi/bolt-python/issues/952),
[bolt-python #470](https://github.com/slackapi/bolt-python/issues/470),
[bolt-python #445](https://github.com/slackapi/bolt-python/issues/445),
[slack-sdk #1110](https://github.com/slackapi/python-slack-sdk/issues/1110),
[slack-sdk #1065](https://github.com/slackapi/python-slack-sdk/issues/1065).

**Mitigations we apply**:
1. **`is_ping_pong_failing()` watchdog** every 60s — forces reconnect on
   positive evidence the socket is stale.
2. **`SlackPoller`** every 15s — calls `conversations.replies` for every
   thread we track and replays any messages we haven't seen via `msg_ts`.
   This is the slack-sdk-team-recommended workaround (external watchdog) and
   the only one that catches the silent-event-drop case where the socket
   technically still passes ping/pong.
3. **systemd `WatchdogSec=600`** — last-resort reset if the whole daemon hangs.

The poller's cost at ~20 named sessions × 4 polls/min = 80 API calls/min,
within Slack's tier-3 rate limit (50+/min per method, with burst tolerance).

If Socket Mode is healthy, the poller has nothing to do — every message has
already been processed by Bolt's `on_message` and recorded in the shared
`delivered_msg_ts` set; the poller's findings are deduped against it.

## Planned migration: Matrix / Element (self-hosted on Proxmox)

Slack's Socket Mode reliability has cost more engineering time than the rest of
this project combined. The poller is a workable mitigation but it's a patch on
a vendor bug we cannot fix. The planned escape hatch:

- **Target**: Matrix server (Synapse or Conduit) running as an LXC container
  on Proxmox, paired with Element on phone + desktop for the user side.
- **Why Matrix over Signal / Nextcloud Talk**:
  - Threads are first-class (replies are organized, like Slack).
  - The bot SDK (`matrix-nio` for Python) is stable and well-maintained.
  - Self-hosted: no rate limits, no zombie WebSockets, no app store gatekeeping.
  - Reactions are first-class.
  - Federation gives an upgrade path if we ever want multi-user.
  - Mobile push notifications work without app-store-tier hurdles.
- **Scope of the port**: only `slack_io.py`, `slack_poller.py`, and the Bolt
  wiring in `__main__.py` are Slack-specific. The worker model, transcript
  reader, registry, liveness check, and Zellij actuator are messaging-agnostic.
  Estimated effort: 1-2 days for a working port.

Trigger for the migration: if the poller fails to recover the bot within
its 15s window more than once a week, port to Matrix.

## Claude Sessions

| Session | Summary | Date |
|---------|---------|------|
| `debug-gemini-routing` | Fixed `rn` blocking for Gemini and verified Slack reply routing via trace logs. | 2026-05-25 |
| `fix-rn-blocking` | Fixed `rn` command not blocking for Gemini; added debug logging to reply router. | 2026-05-25 |
| `run-testing` | Ran full test suite to verify project health; all 57 tests passed. | 2026-05-25 |
| `test-conversation` | Confirmed the Codex session wiring and explained the rename hook block message. | 2026-05-25 |
| `codex-auto-register` | Added Codex/Gemini auto-registration and a portable `rn name` rename prompt. | 2026-05-25 |
| `agent-prefix-routing` | Added Claude/Codex/Gemini labels, optional per-agent channels, and installers for all three agents. | 2026-05-25 |
| `repo-feature-implementation` | Assessed Codex compatibility for the existing Slack/Zellij hook bridge. | 2026-05-25 |
| `slackbot-claude` | Brainstormed, wrote design spec, wrote 15-task plan, implemented all tasks | 2026-05-24 |
