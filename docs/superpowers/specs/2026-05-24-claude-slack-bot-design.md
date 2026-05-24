# claude-slack-bot — Design Spec

**Date:** 2026-05-24
**Status:** Draft (awaiting user approval)
**Author:** Robin Lunde + Claude
**Project location:** `~/git/priv/claude-slack-bot/`

---

## 1. Purpose

Mirror active Claude Code sessions into a Slack workspace so that Robin can:

1. **(Primary)** Receive mobile notifications when CC produces output or asks for input, and reply from his phone — replies are injected back into the actual CC session in his terminal.
2. **(Secondary)** Monitor several parallel CC sessions from a unified inbox without tabbing through Zellij panes.
3. **(Tertiary)** Selectively archive bug-bounty CC conversations as long-lived, searchable Slack threads.

Mirroring is **opt-in per session**: only sessions registered with `/rn <name>` are posted to Slack.

---

## 2. Non-Goals (v1)

- Multiple concurrent CC sessions sharing one project name (one `/rn` name = one active session).
- Offline-reply queue. If the session is not active when a Slack reply arrives, the bot posts `⚠️ session offline, reply not sent` and discards the reply.
- File/image attachments in either direction.
- Auth/allow-list — anyone in the workspace who can post in the channel can drive CC. Acceptable for a personal workspace.
- Rich markdown rendering of CC's full output. Best-effort: code blocks pass through, long bodies truncated with a link to a local file.
- Web UI, dashboards, or analytics.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Host (WSL2 or native Linux)                                │
│                                                             │
│   ┌────────────┐    ┌────────────┐    ┌────────────┐        │
│   │ Zellij     │    │ Zellij     │    │ Zellij     │        │
│   │  pane A    │    │  pane B    │    │  pane C    │        │
│   │ (CC sess)  │    │ (CC sess)  │    │ (CC sess)  │        │
│   └─────┬──────┘    └─────┬──────┘    └─────┬──────┘        │
│         │                 │                 │               │
│         │  hooks: POST localhost:8787/event │               │
│         └─────────────────┼─────────────────┘               │
│                           ▼                                 │
│              ┌────────────────────────┐                     │
│              │ claude-slack-bot        │                     │
│              │ (systemd --user unit)  │                     │
│              │                        │                     │
│              │ - HTTP event endpoint  │                     │
│              │ - SQLite registry      │                     │
│              │ - Slack Socket Mode    │◄──── Slack API ────►│
│              │ - Zellij actuator      │                     │
│              └────────────┬───────────┘                     │
│                           │                                 │
│              zellij action write-chars                      │
│                           ▼ (back to panes A/B/C)           │
└─────────────────────────────────────────────────────────────┘
```

**Three boundaries:**

1. **CC → daemon:** thin bash hook scripts `curl -s --max-time 1` JSON to `http://127.0.0.1:8787/event`. Fire-and-forget; never block Claude.
2. **Daemon ↔ Slack:** `slack-bolt` over Socket Mode. No public URL, no inbound port, no reverse proxy.
3. **Daemon → Zellij:** shells out to `zellij --session NAME action focus-pane-with-id ID` then `action write-chars TEXT` then `action write 13`. Briefly steals pane focus — accepted cost.

---

## 4. Data Model

SQLite file at `${XDG_STATE_HOME:-~/.local/state}/claude-slack-bot/registry.db`.

```sql
CREATE TABLE sessions (
  cc_session_id   TEXT PRIMARY KEY,    -- Claude Code session UUID
  name            TEXT,                -- from /rn; NULL = not mirrored
  cwd             TEXT NOT NULL,
  zellij_session  TEXT,                -- $ZELLIJ_SESSION_NAME at last SessionStart
  zellij_pane_id  TEXT,                -- $ZELLIJ_PANE_ID at last SessionStart
  slack_channel   TEXT,                -- resolved at startup, cached
  slack_thread_ts TEXT,                -- top-level message ts; NULL until named
  created_at      INTEGER NOT NULL,
  last_event_at   INTEGER NOT NULL,
  status          TEXT NOT NULL        -- 'active' | 'ended'
);

CREATE TABLE event_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  cc_session_id   TEXT NOT NULL,
  ts              INTEGER NOT NULL,
  kind            TEXT NOT NULL,       -- 'prompt'|'response'|'notification'|'tools'|'start'|'end'
  payload         TEXT NOT NULL,       -- JSON
  slack_msg_ts    TEXT,                -- set after successful post; NULL = buffered
  FOREIGN KEY (cc_session_id) REFERENCES sessions(cc_session_id)
);

CREATE INDEX idx_event_log_unposted ON event_log(cc_session_id) WHERE slack_msg_ts IS NULL;
```

**Lifecycle:**

| Trigger | Effect |
|---|---|
| `SessionStart` hook | INSERT or UPDATE row by `cc_session_id`. Set/refresh zellij_session, zellij_pane_id, cwd. Set status=active. If row already had a name (resumed session), edit top-level Slack message back to `🟢`. |
| `/rn <name>` (via hook on `UserPromptSubmit` matching the rn command, OR via a dedicated `Notification`-like trigger) | UPDATE name. If first time naming → post top-level message, store slack_thread_ts, replay any buffered events. If renaming → edit top-level message text in place. |
| `UserPromptSubmit` | If name IS NULL → INSERT into event_log with slack_msg_ts=NULL. If name IS NOT NULL → post to thread, store slack_msg_ts. |
| `Stop` (turn ends) | Post Claude's final response + a one-line tool-call summary footer. |
| `Notification` | Post with `⏸ waiting for approval` marker. |
| `SessionEnd` | UPDATE status=ended. Edit top-level message to `⚪ <name>  ·  <cwd>  (ended)`. |

---

## 5. Naming Flow & Resume Behavior

**First time mirroring a session:**

1. CC starts in a Zellij pane → `SessionStart` hook fires → row inserted, name=NULL, status=active. Nothing posted.
2. User runs `/rn my-thing` in CC.
3. The naming mechanism (see §5.1) notifies the daemon: name="my-thing" for this session_id.
4. Daemon posts top-level: `🟢 my-thing  ·  ~/git/priv`. Stores `slack_thread_ts`.
5. Replays any buffered events from `event_log` into the thread in order.
6. Subsequent prompts/responses post live.

**Rename:** `/rn renamed-thing` → daemon edits top-level message to `🟢 renamed-thing  ·  ~/git/priv`. Thread continues unchanged.

**Resume of an existing session (`claude --resume <id>` or auto-restored):**

- Same `cc_session_id` already in registry with a name and `slack_thread_ts`.
- `SessionStart` hook updates `zellij_session`/`zellij_pane_id` to current env, flips status to active.
- Top-level message edited back to `🟢` from `⚪`.
- Events flow into the existing thread. No user action required.

**Reusing a thread for a new session that should continue prior work:**

- User runs `/rn my-thing` on a brand-new `cc_session_id`.
- Daemon finds an existing row with `name='my-thing'` belonging to a different `cc_session_id`.
- Behavior: the old row's name is cleared (`UPDATE sessions SET name=NULL WHERE name='my-thing' AND cc_session_id != new_id`), the new row claims the name AND the existing `slack_thread_ts`. Daemon posts a divider in the thread: `─── 🔄 resumed in new session @ <iso8601> ───`.

### 5.1 How the daemon learns about `/rn`

The `/rn` slash command runs `session_registry.py register-current` directly — it does not fire a CC hook. Two options for catching it:

- **Option A (recommended):** A `UserPromptSubmit` hook script inspects the prompt text. If it matches the `/rn <name>` pattern OR if the system-reminder after slash-command execution contains "renamed", the hook POSTs a `name` event to the daemon with `{"name": "<value>"}`.
- **Option B:** Wrap `session_registry.py` to also POST to the daemon. Tightly couples to the existing tool — rejected.

Spec chooses **Option A**.

---

## 6. Events Posted to Slack

| Event | Posted? | Rendering |
|---|---|---|
| `UserPromptSubmit` | Yes | `👤 <prompt text>` (in code block if multi-line) |
| `Stop` (final assistant text) | Yes | `🤖 <response text>` (code block if multi-line, truncated >3000 chars with `📎 full text in /tmp/cc-<sid>-<ts>.md` written to host) |
| Tool calls within a turn | Summarized only | Appended to the `Stop` post as footer: `↳ 3 reads · 1 edit · 2 bash` |
| `Notification` | Yes | `⏸ <message>` — meant for permission prompts and "needs attention" pings |
| Session start (new, never named) | No | (nothing until `/rn`) |
| Session start (resume of named session) | Yes | Edit top-level back to `🟢`; no thread message |
| `SessionEnd` | Yes | Edit top-level to `⚪ ... (ended)`; no thread message |
| Errors (tool failures bubbling to user) | Yes | `❌ <error>` |
| Transient retries / internal errors | No | Daemon logs only |

**Verbosity toggle:** `CC_SLACK_VERBOSE=tools` env var on the daemon to also post each tool call individually. Default off.

---

## 7. Slack Side

### App setup (one-time, documented in README)

1. Create a Slack app at api.slack.com/apps in Robin's workspace.
2. Enable **Socket Mode** → generates app-level token `xapp-...`.
3. **Bot scopes:** `chat:write`, `chat:write.public`, `channels:history`, `groups:history`, `reactions:write`, `commands`, `app_mentions:read`.
4. **Event subscriptions:** `message.channels`, `message.groups`, `app_mention`.
5. Install to workspace → bot token `xoxb-...`.
6. Invite the bot user to the target channel (default `#claude-code`, configurable).

### Slash commands

| Command | Behavior |
|---|---|
| `/cc-list` | Reply (ephemeral) with table of active sessions: name, cwd, last_event_at, thread permalink |
| `/cc-mute <name>` | Toggle mute for a session — daemon will not post until unmuted |
| `/cc-status` | Daemon health: uptime, registered session count, last error |

### Reply handling

- Bot subscribes to `message.channels` / `message.groups`.
- Filter: `thread_ts` must match a known `slack_thread_ts`; ignore messages from the bot itself; ignore top-level messages.
- Lookup `cc_session_id` and `zellij_session`/`zellij_pane_id`.
- If session status='ended' → post in-thread `⚠️ session offline, reply not sent`. React 🚫 on the user's message.
- Else → call zellij actuator. On success, react ✅. On failure, post `❌ delivery failed: <reason>` and react ⚠️.

---

## 8. Hooks

All hook scripts live in `claude-slack-bot/hooks/` in the repo and are installed by `hooks/install.sh` into `~/.claude/hooks/claude-slack-bot/` with idempotent edits to `~/.claude/settings.json`.

| File | CC hook | Payload sent to daemon |
|---|---|---|
| `session_start.sh` | `SessionStart` | `{kind:"start", session_id, cwd, zellij_session, zellij_pane_id, resumed:bool}` |
| `prompt.sh` | `UserPromptSubmit` | `{kind:"prompt", session_id, text}` — also detects `/rn <name>` and additionally posts `{kind:"name", session_id, name}` |
| `stop.sh` | `Stop` | `{kind:"response", session_id, text, tool_summary}` |
| `notify.sh` | `Notification` | `{kind:"notification", session_id, message}` |
| `session_end.sh` | `SessionEnd` | `{kind:"end", session_id, reason}` |

**Hook template (bash):**

```bash
#!/usr/bin/env bash
set -uo pipefail   # no -e: never fail a hook on daemon downtime
payload="$(jq -n --arg sid "$CLAUDE_SESSION_ID" '{...}')"
curl -fsS --max-time 1 -H 'content-type: application/json' \
     -d "$payload" http://127.0.0.1:8787/event >/dev/null 2>&1 || true
exit 0
```

The exact CC hook env vars and stdin shape will be confirmed during implementation against current Claude Code docs.

---

## 9. Repo Layout

```
claude-slack-bot/
├── README.md                       # setup, run, troubleshoot
├── pyproject.toml                  # uv-managed, deps pinned
├── .python-version                 # 3.13
├── Containerfile                   # documented future option
├── compose.yaml                    # documented future option
├── .env.example                    # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL_ID, PORT
├── systemd/
│   └── claude-slack-bot.service     # user unit; installed to ~/.config/systemd/user/
├── src/slackbot/
│   ├── __main__.py
│   ├── server.py                   # aiohttp HTTP + slack-bolt Socket Mode
│   ├── registry.py                 # SQLite layer (sync, single-writer)
│   ├── slack_io.py                 # post/edit/react formatters
│   ├── zellij_io.py                # actuator
│   ├── events.py                   # per-kind handlers
│   ├── config.py                   # env-driven Config dataclass
│   └── logging_setup.py            # respects LOG_LEVEL
├── hooks/
│   ├── session_start.sh
│   ├── prompt.sh
│   ├── stop.sh
│   ├── notify.sh
│   ├── session_end.sh
│   └── install.sh                  # idempotent install + settings.json patch
└── tests/
    ├── test_registry.py
    ├── test_events.py
    ├── test_slack_io.py
    ├── test_zellij_io.py
    └── test_integration.py         # fake zellij, mock slack
```

---

## 10. Configuration

All via env vars, loaded from `~/.config/claude-slack-bot/env` (mode 0600) by the systemd unit:

| Var | Required | Default | Meaning |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | yes | — | `xoxb-...` |
| `SLACK_APP_TOKEN` | yes | — | `xapp-...` (Socket Mode) |
| `SLACK_CHANNEL_ID` | yes | — | Channel ID where threads are posted |
| `SLACKBOT_PORT` | no | `8787` | Local HTTP port for hook events |
| `SLACKBOT_DB_PATH` | no | `$XDG_STATE_HOME/claude-slack-bot/registry.db` | SQLite path |
| `SLACKBOT_TMP_DIR` | no | `/tmp` | Where truncated bodies are spooled |
| `CC_SLACK_VERBOSE` | no | `off` | `off`\|`tools` |
| `LOG_LEVEL` | no | `INFO` | Standard Python logging level |

---

## 11. Deployment

**Primary path: systemd user unit**

```ini
# ~/.config/systemd/user/claude-slack-bot.service
[Unit]
Description=Slackbot bridge for Claude Code sessions
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/claude-slack-bot/env
ExecStart=%h/git/priv/claude-slack-bot/.venv/bin/python -m slackbot
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

Enable: `systemctl --user enable --now claude-slack-bot`.

Works identically on WSL2 (systemd-in-WSL is already enabled per project CLAUDE.md) and native Linux.

**Optional path: container**

`Containerfile` + `compose.yaml` provided but not used by default. Container would need:
- `--network=host` to receive localhost hook events
- A host-side `zellij` shim because the zellij CLI talks to the user's terminal multiplexer, which is not accessible from inside a container

The container option is documented as a future direction; v1 ships systemd-only.

---

## 12. Testing

| Layer | Approach |
|---|---|
| Unit | `pytest` on `registry.py` (in-memory SQLite), `events.py` (pure functions), `slack_io.py` formatters (snapshot), `zellij_io.py` (shells against a fake `zellij` binary on PATH) |
| Integration | Spin up the daemon against a mock Slack client (`slack_sdk` test helpers) and a fake `zellij` shim. End-to-end: post `prompt` event → assert Slack post; simulate Slack reply → assert `zellij` shim was called with correct args |
| Manual smoke | Documented in README: start daemon, `/rn smoke`, send a prompt, see thread appear, reply in Slack, confirm text typed into pane |

Linters: `ruff check` + `ruff format` + `shellcheck` on all `.sh` files. Zero-warnings policy.

---

## 13. Open Risks

| Risk | Mitigation |
|---|---|
| Focus-stealing on reply injection | Documented as known behavior. May add a "preview mode" later that requires re-focus opt-in. |
| Detecting `/rn` via prompt-text regex is fragile | Acceptable. The `/rn` command body is stable in Robin's setup. Pattern lives in `prompt.sh` and is one-line-easy to update. |
| Slack rate limits | `slack-bolt` has built-in backoff. Event volume is low (single user, ~tens of posts/hour peak). |
| Secrets leakage | Tokens live only in `~/.config/claude-slack-bot/env` (mode 0600), never in repo. `.env.example` has placeholders only. |
| Daemon crash during a turn loses one post | systemd `Restart=always`. Worst case: one event missed; SQLite-persisted state recovers cleanly. No retry queue. |
| Multi-session-per-project | Out of scope v1 (assumed away). Schema already keys on `cc_session_id`, so future expansion to multi-session per name is a presentation-layer change. |

---

## 14. Deliverables

1. `claude-slack-bot/` directory in `~/git/priv/` with all source, hooks, tests, systemd unit, README.
2. Slack app setup walkthrough in README.
3. `hooks/install.sh` that idempotently merges hook registrations into `~/.claude/settings.json`.
4. Working end-to-end: register session → events mirrored → Slack reply types into pane.
5. README.md with all 10 mandatory sections per project CLAUDE.md.

---

## 15. Out of Scope (Explicit)

- Multi-user / shared workspace use
- Multi-session per project name
- Offline reply queueing
- File or image transfer either direction
- Slack-side editing of past messages reflecting into CC history
- Replay of CC sessions from Slack archive
- Web UI, dashboards, metrics export
