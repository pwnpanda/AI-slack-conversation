# claude-slack-bot

Bridge between Claude Code sessions and Slack: mirrors selected CC sessions into Slack threads, and types your Slack replies back into the originating Zellij pane.

## What this does

Registers a Claude Code session with `/rn <name>` to opt it into Slack mirroring. The bot posts a top-level message naming the session and mirrors every subsequent prompt, response, and notification into the thread beneath it. Replying in that Slack thread types your reply into the originating Zellij pane (via `zellij action write-chars`).

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
- Claude Code CLI

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

### 4. Install the CC hooks

```bash
bash ~/git/priv/claude-slack-bot/hooks/install.sh
```

This copies the five hook scripts into `~/.claude/hooks/claude-slack-bot/` and merges hook entries into `~/.claude/settings.json`.

## Usage

1. Start Claude Code inside a Zellij pane
2. Run `/rn my-session` — top-level message appears in your configured Slack channel
3. Type prompts in CC; they mirror into the Slack thread
4. From Slack (mobile or desktop), reply in the thread — the text is typed into the CC pane (CC pane briefly takes focus)

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
- Event buffering for prompts that arrive before `/rn`
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

## Claude Sessions

| Session | Summary | Date |
|---------|---------|------|
| `slackbot-claude` | Brainstormed, wrote design spec, wrote 15-task plan, implemented all tasks | 2026-05-24 |
