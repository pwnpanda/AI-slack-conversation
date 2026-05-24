# claude-slack-bot

Bridge between Claude Code sessions and Slack: mirrors selected CC sessions into Slack threads, and types your Slack replies back into the originating Zellij pane.

**Status:** design phase. Spec at [`docs/superpowers/specs/2026-05-24-claude-slack-bot-design.md`](docs/superpowers/specs/2026-05-24-claude-slack-bot-design.md).

## What this does

When you register a Claude Code session with `/rn <name>`, this daemon posts a top-level message in a Slack channel and mirrors all subsequent prompts, responses, and notifications into the thread under it. Replying in that thread types your message into the Claude Code pane via Zellij.

## Use cases

- Mobile notifications when CC asks for input or finishes a long-running turn
- Reply to CC from your phone — the text is typed into the actual terminal session
- Monitor multiple parallel CC sessions without tabbing through Zellij panes
- Long-lived searchable archive of selected sessions (e.g. bug-bounty work)

## Requirements

To be filled out as the project is implemented. Target stack:
- Python 3.13, `uv`-managed
- Zellij 0.44+
- Slack workspace with a bot app (Socket Mode)
- systemd (user units) on Linux or WSL2 with systemd enabled

## Installation / Setup

Not yet implemented.

## Usage

Not yet implemented.

## Testing

Not yet implemented.

## Deployment

Planned: `systemd --user` unit. Container option documented but optional.

## Implemented features

None yet — design phase.

## Planned features

See the design spec linked above for the full v1 scope.

## Claude Sessions

| Session | Summary | Date |
|---------|---------|------|
| `slackbot-claude` | Brainstormed and wrote v1 design spec for the bridge daemon | 2026-05-24 |
