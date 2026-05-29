# Matrix migration plan

Port `claude-slack-bot` from Slack Socket Mode to a self-hosted Matrix homeserver. Single-user deploy on Proxmox, Element X on Android, push via self-hosted ntfy + UnifiedPush.

## Status check (2026-05-28)

- **conduwuit** is no longer the upstream — the original author archived it; the community fork **Continuwuity** at https://forgejo.ellis.link/continuwuation/continuwuity is the active continuation (30 releases, RocksDB-backed, Rust). The GitHub mirror is `continuwuity/continuwuity`. Plan targets Continuwuity. (Synapse remains a viable fallback if Continuwuity stalls.)
- **matrix-nio** 0.25.2 on PyPI, actively maintained, threads supported via `m.thread` relation in `Api.room_send` content. E2EE optional via `matrix-nio[e2e]` + libolm.
- **Element X Android** supports UnifiedPush; ntfy ships a built-in `/_matrix/push/v1/notify` gateway. Documented path: Element-X picks ntfy distributor on first login.
- **Matrix threads** are stable since spec v1.4 (MSC3440); Element X renders threads natively.

## 1. Goals & non-goals

**Goals.**
- Replace Slack as the transport for posting agent session top-levels, mirroring prompts/responses into threads, and accepting thread replies that are typed into the Zellij pane.
- Eliminate the Socket Mode silent-failure class of bug. Matrix `/sync` returns HTTP errors that are observable; no separate poller.
- Push notifications on Android within ~5s of an agent event, without Google FCM.

**Non-goals (v1).**
- Federation. Server is closed (`allow_federation = false`).
- Multi-user / group chats. One bot user, one human user.
- E2EE. Out for v1 — adds libolm dep and key management overhead; the homeserver is on the user's own LAN. Revisit later.
- iOS client. Element X iOS uses APNs via Element's sygnal; not in scope.
- Slack parity for niche features (slash commands beyond `/rn`, `@mentions`).

## 2. Architecture overview

Current: Slack Bolt + AsyncSocketModeHandler keeps a WebSocket to slack.com; `SlackPoller` polls `conversations.replies` every 15s as a watchdog against Socket Mode dropping events; `SlackIO` posts/edits/reacts via `chat.postMessage`/`chat.update`/`reactions.add`.

After: a `matrix-nio` `AsyncClient` runs `sync_forever` against a local Continuwuity LXC; `MatrixIO` replaces `SlackIO`; `sync_forever`'s built-in retry + observable HTTP errors replace the entire `SlackPoller`. Workers, supervisor, transcript reader, registry, liveness, Zellij actuator, hooks, HTTP event endpoint — all unchanged.

Ingress chain after the proxy swap (see §11):

```
                                                      ┌─► Continuwuity :6167 (HTTP)
Element X (cellular) ──HTTPS──► router :443 ──► Caddy LXC ─┤
                                                      └─► ntfy :2586 (HTTP)
                                              chat.robinlunde.com / push.robinlunde.com
```

Daemon flow (unchanged shape, MatrixIO swap):

```
Agent hook → POST /event → daemon ─┐
                                   ├─ MatrixIO ──HTTPS──► Continuwuity LXC ─push─► ntfy (same LXC) ─UP─► Element X
Zellij pane ◄── ZellijActuator ◄───┘                            ▲                                       │
                                                                └────────── /sync (long-poll) ───────────┘
```

## 3. Infrastructure setup (Proxmox)

One LXC for Continuwuity + ntfy co-located (no inter-LXC network hops; saves one reverse-proxy hop and a TLS cert; both are lightweight). Sibling LXC was considered and rejected — no isolation benefit for single-user.

- **LXC**: Debian 12, unprivileged, 2 vCPU / 2 GB RAM / 8 GB disk. Continuwuity steady-state is well under 200 MB; RocksDB grows slowly for a single-user server.
- **Hostnames / DNS**: `chat.robinlunde.com` (homeserver) and `push.robinlunde.com` (ntfy). Two A records pointing at the home IP, kept fresh by the existing DDNS automation. Wildcard cert on the existing Caddy LXC already covers `*.robinlunde.com`.
- **Reverse proxy**: **existing Caddy LXC** (made the sole public hit per §11). The new Matrix LXC has no public-facing process — Caddy reverse-proxies HTTP from the LAN side, terminating TLS at the edge with the existing wildcard.
- **TLS**: terminated at Caddy. The Matrix LXC stays HTTP-only on the LAN interface — no cert, no ACME, no certbot. Cert renewals continue under Caddy's existing flow.
- **Continuwuity config** (`/etc/continuwuity/continuwuity.toml`):
  - `server_name = "chat.robinlunde.com"`
  - `allow_federation = false`
  - `allow_registration = false` (provision the bot + user accounts via admin API)
  - `database_backend = "rocksdb"` (default; verify on release notes for the version you install)
  - `address = "0.0.0.0"`, `port = 6167` — listens on the LAN interface so the Caddy LXC can reach it. (Firewall the LXC at the Proxmox level to only allow the Caddy LXC IP, since there is no TLS protecting LAN-side traffic.)
- **`.well-known` discovery**: Element X probes `https://chat.robinlunde.com/.well-known/matrix/client`. Either let Continuwuity serve it directly (configurable) or serve a static JSON from Caddy: `{"m.homeserver":{"base_url":"https://chat.robinlunde.com"}}`. Both work; Continuwuity-served is one fewer config file.
- **Backups**: nightly `systemctl stop continuwuity && tar czf …/rocksdb-$(date).tar.gz /var/lib/continuwuity && systemctl start` to the Proxmox backup volume. RocksDB does not tolerate hot copies. Single-user, so the downtime is irrelevant.
- **Systemd unit**: install the upstream `.service` from the release tarball; the project ships one. Type=notify, Restart=on-failure.
- **Health check** (from LAN): `curl -fsS http://<matrix-lxc-ip>:6167/_matrix/client/versions | jq .versions`. From outside: `curl -fsS https://chat.robinlunde.com/_matrix/client/versions | jq .versions`. Both must return a non-empty `versions` list.

Upstream install docs: https://continuwuity.org/ — follow their Debian instructions rather than re-deriving them here.

## 4. ntfy + UnifiedPush

Element X needs a push gateway because the app cannot maintain a background WebSocket to Matrix on Android. UnifiedPush + ntfy avoid Google FCM entirely; the homeserver POSTs to the ntfy `/_matrix/push/v1/notify` endpoint, ntfy fans out to the Android distributor app, which wakes Element X.

- **Deploy**: same LXC as Continuwuity. ntfy is a single Go binary; `apt install ntfy` on Debian 12 or download release.
- **Config** (`/etc/ntfy/server.yml`):
  - `base-url: "https://push.robinlunde.com"` (required for the Matrix gateway to function)
  - `listen-http: "0.0.0.0:2586"` (LAN-side so the Caddy LXC can reach it)
  - `behind-proxy: true`
  - Disable the web app (`web-root: disable`) — single-user, not needed.
- **Caddy site** (on the existing Caddy LXC, added alongside the existing wildcard-cert config):
  ```caddy
  push.robinlunde.com {
      reverse_proxy http://<matrix-lxc-ip>:2586 {
          flush_interval -1
      }
  }
  ```
- **Verify**: `curl https://push.robinlunde.com/_matrix/push/v1/notify` returns `{"unifiedpush":{"gateway":"matrix"}}`.
- **Android client**: install ntfy app from F-Droid; settings → server URL = `https://push.robinlunde.com`. Then install Element X; on first login it autodetects ntfy as the UnifiedPush distributor. Disable battery optimisation for both apps (see dontkillmyapp.com guidance referenced in Element's docs).

Reference: https://docs.element.io/latest/element-support/element-androidios-client-settings/using-unified-push-and-ntfy-for-push-notifications/

## 5. Room layout

**Decision: one room per agent**, threads per session. This matches the existing Slack-per-agent-channel model (`SLACK_CHANNEL_ID_CLAUDE` / `_CODEX` / `_GEMINI` in `config.py:39-45`) and keeps the registry schema near-isomorphic: `slack_channel` → `matrix_room_id`, `slack_thread_ts` → `matrix_thread_root_event_id`.

- Create 3 rooms: `#claude`, `#codex`, `#gemini`, plus one fallback room for unrouted agents. All private, invite-only, the bot + user are the only members.
- Threads: send messages with content
  ```json
  {"msgtype":"m.text","body":"...","m.relates_to":{"rel_type":"m.thread","event_id":"<root_event_id>"}}
  ```
  Element X renders these as threads. Spec stable since v1.4 / MSC3440.
- **Accounts**: one bot account (e.g. `@cc-bot:matrix.<domain>`) and one user account (`@robin:matrix.<domain>`). Provision both via Continuwuity's admin command room (the server creates a `#admins` room on first start; commands like `!admin create-user` are documented in the Continuwuity admin docs). Issue an access token for the bot via `POST /_matrix/client/v3/login` with `type=m.login.password` and store it in the env file.

## 6. Code port plan

Concrete, file-by-file. Line refs are to the current tree.

### `src/slackbot/slack_io.py` → `src/slackbot/matrix_io.py`

Replace 1:1. Same method names, same return-string contract (callers treat the returned id as opaque). `nio.AsyncClient.room_send` is the workhorse; it returns a `RoomSendResponse` with `event_id`.

| Existing method (slack_io.py:11-50) | Matrix replacement |
|---|---|
| `channel_for_agent(agent)` | unchanged — returns `room_id` instead of channel id |
| `post_top_level(text, channel)` | `client.room_send(room_id, "m.room.message", {"msgtype":"m.text","body":text})` → return `resp.event_id` |
| `post_in_thread(thread_ts, text, channel)` | same call with `m.relates_to.rel_type="m.thread"` + `event_id=thread_ts` (rename param to `thread_root` internally) |
| `edit_top_level(ts, text, channel)` | `room_send` with `m.new_content` + `m.relates_to.rel_type="m.replace"` + `event_id=ts`; also set top-level `body` to a fallback `"* <text>"` per the spec |
| `react(ts, emoji, channel)` | `room_send` type `m.reaction` with `m.relates_to.rel_type="m.annotation"`, `event_id=ts`, `key=<unicode emoji>`. Swallow `MatrixRequestError` as today (slack_io.py:43-50). |

Map the existing emoji names (`white_check_mark`, `warning`, `no_entry_sign`) to the Unicode codepoints Element X already renders: `✅`, `⚠️`, `🚫`.

### `src/slackbot/slack_poller.py` → delete

`/sync` errors are observable: `sync_forever` raises or returns an error response; we log and let `Restart=on-failure` handle it. No second watchdog needed. The systemd `WatchdogSec=600` plus `sd_notify.watchdog()` on every sync token tick is enough.

### `src/slackbot/__main__.py`

Replace lines 9-13 (slack-bolt/slack-sdk imports) and the Bolt wiring at 105-143:

- Drop `AsyncApp`, `AsyncSocketModeHandler`, `AsyncWebClient`, `socket_health_watchdog`, `SlackPoller`.
- Construct: `client = AsyncClient(cfg.homeserver_url, cfg.matrix_user_id, store_path=cfg.matrix_store_dir, device_id=cfg.matrix_device_id)`; `await client.restore_login(user_id, device_id, access_token)`.
- Register a callback: `client.add_event_callback(on_room_message, RoomMessageText)`.
- In `on_room_message(room, event)`: ignore self (`event.sender == client.user_id`), require `event.source["content"].get("m.relates_to", {}).get("rel_type") == "m.thread"`, extract `thread_root = m.relates_to.event_id`, then call the same `handle_thread_reply(room.room_id, thread_root, event.body, event.event_id)` as today. The dedupe set keyed on `event_id` (line 110-120) carries over unchanged.
- Start: `tasks.append(asyncio.create_task(client.sync_forever(timeout=30000, full_state=False)))` — replaces `socket_handler.start_async()` at line 170.
- Keep: `reader_pump`, `reaper`, `watchdog_heartbeat`, supervisor re-attach loop (lines 155-161), HTTP runner (lines 144-149), signal handling.
- Call `sd_notify.watchdog()` from inside `on_room_message` and from a wrapped sync callback that fires every successful `/sync` (nio exposes `client.add_response_callback(_, SyncResponse)`).

### `src/slackbot/config.py`

Replace lines 12-25, 48-62. New fields:

```python
matrix_homeserver: str   # MATRIX_HOMESERVER, e.g. https://matrix.<domain>
matrix_user_id: str      # MATRIX_USER_ID, e.g. @cc-bot:matrix.<domain>
matrix_access_token: str # MATRIX_ACCESS_TOKEN
matrix_device_id: str    # MATRIX_DEVICE_ID — persisted for /sync continuity
matrix_store_dir: str    # default: $XDG_STATE_HOME/claude-slack-bot/nio-store
matrix_default_room: str # MATRIX_ROOM_ID — fallback for unmapped agents
agent_rooms: dict[str, str]  # MATRIX_ROOM_ID_CLAUDE / _CODEX / _GEMINI
```

`agent_channels` → `agent_rooms`, same lookup pattern. Keep `port`, `db_path`, `tmp_dir`, `verbose`, `log_level`, `stale_after_seconds` unchanged. Env file location (`~/.config/claude-slack-bot/env`) unchanged.

### `src/slackbot/registry.py`

Schema rename in `_SCHEMA` (registry.py:11-26):

- `slack_channel TEXT` → `matrix_room_id TEXT`
- `slack_thread_ts TEXT` → `matrix_thread_root TEXT`
- `event_log.slack_msg_ts TEXT` → `event_log.matrix_event_id TEXT`
- index `idx_event_log_unposted` updated accordingly.

Migration: this is a single-user deploy, the existing DB has no value after the cutover. Delete `~/.local/state/claude-slack-bot/registry.db` and let the new schema create fresh. Document in README. (Considered an `_migrate` ALTER chain like registry.py:82-95; rejected — extra code for zero benefit since old Slack thread IDs are meaningless under Matrix.)

Rename methods: `set_thread_ts` → `set_matrix_thread_root`; `get_session_by_thread` → `get_session_by_matrix_thread`; `list_threads` already only checks `IS NOT NULL`, just rename the column reference.

### `src/slackbot/handlers.py`, `supervisor.py`, `worker.py`, `reply_router.py`

Touch only call sites where the field/method names changed. No semantic changes. The "thread_ts is opaque string" abstraction in these files (handlers.py and worker.py treat it as a black box) means the diff is mechanical.

### Tests

- `tests/test_slack_io.py` → `tests/test_matrix_io.py`: replace the `AsyncWebClient` fake with a `nio.AsyncClient`-shaped fake (just needs `room_send` returning an object with `.event_id`).
- `tests/test_slack_poller.py` — delete.
- `tests/conftest.py` — rename fixtures from `slack_*` to `matrix_*`.
- `tests/test_registry.py` — column name updates.
- `tests/test_handlers.py`, `test_reply_router.py`, `test_supervisor.py`, `test_worker.py`, `test_integration_worker.py` — `SlackIO` fake → `MatrixIO` fake. Method signatures unchanged so the bodies don't move.

### pyproject.toml / uv.lock

- Remove `slack-sdk`, `slack-bolt`.
- Add `matrix-nio>=0.25.2` (no `[e2e]` extra for v1).
- `uv lock && uv sync`.

### Hooks

`hooks/*.sh` — no changes. They only POST to `http://127.0.0.1:8787/event`. Re-run installers after the daemon is rebuilt to confirm idempotency.

## 7. Rollout

Hard-cut. The two transports cannot share thread IDs, dual-posting doubles registry complexity, and there is one user. Procedure:

1. Land the port on a branch `matrix-port`. Keep `main` on Slack until cutover.
2. Stand up the LXC; provision rooms; verify §8 checklist using a throwaway daemon instance with `SLACKBOT_PORT=8788`.
3. Stop the Slack daemon (`systemctl --user stop claude-slack-bot`).
4. Switch the systemd unit's env file from Slack vars to Matrix vars, deploy the new code, start.
5. Keep `main`'s Slack branch alive in git for 7 days as the rollback path. If Matrix is broken at any point during that week, `git checkout main && systemctl --user restart` returns to Slack within a minute.
6. After 7 days of stable Matrix operation, delete the Slack code (`slack_io.py`, `slack_poller.py`, Slack tests, Slack env docs) on `main` per the "replace, don't deprecate" rule in `~/.claude/CLAUDE.md`.

## 8. Verification checklist

Run all of these against the new daemon before declaring the cutover done.

- [ ] Agent session start → top-level message appears in the agent's room with the session name and 🟢 prefix.
- [ ] `/rn rename` (Claude) or `rn rename` (Codex/Gemini) → top-level message edited with new name; thread is reused.
- [ ] Agent prompt → mirrored as a threaded message under the top-level.
- [ ] Agent response → mirrored as a threaded message.
- [ ] Agent notification (Stop hook) → posted in thread; on next event, resolved-marker edit is applied to the right event.
- [ ] Reply from Element X mobile thread → text appears typed in the Zellij pane (pane briefly focuses).
- [ ] ✅ reaction appears on a delivered reply; ⚠️/🚫 appear on degraded paths.
- [ ] Restart the daemon mid-conversation (`systemctl --user restart`) → transcript readers re-attach (existing logic in `__main__.py:155-161`), no duplicate posts (dedupe by `matrix_event_id`).
- [ ] Element X push notification arrives on the phone within 5 seconds with the app backgrounded.
- [ ] `journalctl --user -u claude-slack-bot -f` shows no warnings during a 10-minute idle window.
- [ ] Kill the LXC briefly (`pct stop` then `pct start`) → daemon reconnects via `sync_forever`'s retry loop; no manual intervention.

## 9. Effort estimate

| Section | Hours |
|---|---|
| LXC + Continuwuity + Caddy install | 2 |
| ntfy + Element X push wiring + Android testing | 2 |
| Room/account provisioning, access token | 0.5 |
| `MatrixIO` + tests | 2 |
| `__main__.py` rewrite (callback wiring, sync_forever) | 2 |
| `config.py` + `registry.py` schema rename + tests | 1.5 |
| Touch-up across handlers/worker/router + test fakes | 1.5 |
| Verification checklist run-through, fix-ups | 2 |
| README + env example rewrite | 1 |
| **Total** | **~14.5 h (≈2 working days)** |

## 10. Decisions & open questions

**Resolved.**
- **Ingress.** Port forward `:443` → Caddy LXC. DDNS keeps the home IP fresh. Existing wildcard cert on Caddy covers both subdomains. (No Cloudflare Tunnel, no Tailscale Funnel.)
- **Hostnames.** `chat.robinlunde.com` for Continuwuity, `push.robinlunde.com` for ntfy. Both proxied by the existing Caddy LXC.
- **TLS termination.** At the existing Caddy LXC. Matrix LXC stays HTTP-only on the LAN interface; firewalled to only accept connections from the Caddy LXC IP.
- **Proxy chain.** Caddy becomes the sole public hit; HA NPM retired from the public path (see §11 for the swap procedure and rollback).
- **Emoji set.** `✅ ⚠️ 🚫` Unicode glyphs (Element X renders cleanly, matches the Slack semantics 1:1).
- **E2EE for v1.** No. Skip libolm / key management; revisit once everything else is stable.
- **Room layout.** Per-agent rooms (`#claude`, `#codex`, `#gemini` + fallback), one thread per session. Matches the existing channel split and gives per-agent mobile mute granularity.

**Still open.**
- **Backup destination.** Proxmox PBS, or a separate target?
- **Bot username.** `@cc-bot`? `@agent`? Affects readability of the `[Claude]` etc. label — could be dropped if each agent has its own room.
- **Federation later.** Closed for v1; if you ever want it on, `allow_federation` cannot be flipped cleanly per Continuwuity's warning. Decide now whether to leave the door open.

## 11. Caddy-first ingress: swap procedure, risks, and pre-cutover checks

The Matrix migration assumes the public ingress is **Caddy**, not Home Assistant's Nginx Proxy Manager. This swaps the order of the existing chain (`Internet → HA NPM → Caddy → service` becomes `Internet → Caddy → service`). The rationale is in the migration design notes; this section is the operational checklist.

### Swap procedure

1. **Snapshot the Caddy LXC** in Proxmox first. A broken Caddyfile becomes a global outage now that Caddy is the only ingress.
2. **Migrate site blocks**: for every HA NPM proxy host currently exposed publicly, add an equivalent Caddy site block:
   ```caddy
   <hostname>.robinlunde.com {
       reverse_proxy <upstream-host>:<port>
   }
   ```
   Keep the wildcard ACME config that's already producing certs; nothing changes on the cert side.
3. **Add the two new Matrix site blocks** (with the `/sync` long-poll and SSE settings):
   ```caddy
   chat.robinlunde.com {
       reverse_proxy http://<matrix-lxc-ip>:6167 {
           flush_interval -1
           transport http {
               read_timeout 5m
           }
       }
   }

   push.robinlunde.com {
       reverse_proxy http://<matrix-lxc-ip>:2586 {
           flush_interval -1
       }
   }
   ```
4. **Internal-only sites** (anything currently behind Caddy that should never be public): bind to the LAN interface so the listener physically cannot accept WAN traffic, regardless of DNS:
   ```caddy
   (internal) {
       bind <caddy-lan-ip>
   }

   internal-service.robinlunde.com {
       import internal
       reverse_proxy <upstream>:<port>
   }
   ```
   Re-test the leak check below whenever a new internal site is added.
5. **Validate before reload**: `caddy validate --config /etc/caddy/Caddyfile`. Reload only on a green validate.
6. **Reload Caddy**, confirm the existing public hostnames still serve, then **flip the router port-forward** `:443` from the HA NPM LXC IP to the Caddy LXC IP. (And `:80` if you keep an HTTP→HTTPS redirect.)
7. **Stop HA NPM from accepting public traffic.** Either stop the add-on or remove the proxy hosts from its config. Keep the add-on installed for one week as a rollback path — flipping the router back is a 1-minute operation if something breaks.
8. **After one week of stable Matrix operation**: remove the HA NPM add-on if nothing else in HA references it.

### Risks to verify after the swap

Each risk has a one-line check you can run to confirm the safe state. Re-run before and after the cutover, and whenever Caddyfile changes touch ingress.

- **ACME DNS-01 token scope.** The wildcard cert renewal token can rewrite DNS for the whole zone if not scoped. **Check**: `grep acme_dns /etc/caddy/Caddyfile` returns the expected provider + token env var; if your DNS provider supports it, scope the API token to `_acme-challenge` records only.
- **`/sync` long-poll timeouts.** Element X opens a `/sync` request with a ~60s timeout. If Caddy closes the connection earlier, clients reconnect in a loop and push notifications fall behind. **Check**: `journalctl -u caddy | rg 'context deadline'` returns nothing in steady-state idle. The `flush_interval -1` + `read_timeout 5m` lines above prevent this; deleting either reintroduces the bug.
- **Federation reachability.** Federation is off for v1, but if you ever turn it on, the `.well-known` and SRV records need to point clients at port 443 over the public hostname. **Check**: `curl -fsS https://chat.robinlunde.com/.well-known/matrix/server | jq` returns `{"m.server":"chat.robinlunde.com:443"}` (or whatever you configured). The [federation tester](https://federationtester.matrix.org/) confirms end-to-end.
- **Internal-site leak.** A misconfigured site block could expose an internal service publicly. **Check** (run from off-LAN — phone on cellular works):
  ```bash
  curl -v -H "Host: internal-service.robinlunde.com" https://<public-IP>/
  ```
  Should return a TLS error or 404 — never the service body. The `bind <lan-ip>` directive is what guarantees this; re-test whenever a new internal site is added.
- **Single point of failure.** Caddy is now the only ingress; a bad Caddyfile takes everything down. **Check**: `caddy validate --config /etc/caddy/Caddyfile && echo OK` returns OK before every reload. Wire this into your edit workflow (pre-commit hook, alias, whatever). Snapshot the Caddy LXC before non-trivial changes.
- **HA add-on removal.** Before deleting the HA NPM add-on, confirm nothing in HA's automations or scripts calls its API. **Check**: `grep -r "nginx-proxy-manager\|<npm-internal-host>" /config/` in the HA config volume returns no matches.
- **Matrix LXC firewall.** Matrix LXC is HTTP-only on the LAN; if any other host can reach `:6167`/`:2586` they get unencrypted access to your homeserver. **Check** (from a non-Caddy LXC):
  ```bash
  curl --max-time 3 http://<matrix-lxc-ip>:6167/_matrix/client/versions
  ```
  Should time out or refuse. Allow only the Caddy LXC IP via Proxmox firewall.
- **Sync watchdog still wired.** With `SlackPoller` deleted, the only liveness signal is `sync_forever` raising. Make sure `sd_notify.watchdog()` is called from the sync-response callback (see §6 `__main__.py` notes); otherwise `WatchdogSec=600` will restart the daemon every 10 minutes during legitimate quiet periods.
- **Push delivery latency.** If push falls behind, the cause is usually ntfy or UnifiedPush, not Caddy. **Check**: post a test event via the bot, watch `journalctl -u ntfy -f` for the inbound POST and the outbound UP delivery within a second of the event. Element X should fire within 5s on the phone.
