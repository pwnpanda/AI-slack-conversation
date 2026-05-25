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
