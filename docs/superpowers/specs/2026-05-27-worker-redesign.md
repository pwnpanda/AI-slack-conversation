# Worker-Per-Conversation Redesign — Design Spec

**Date:** 2026-05-27
**Status:** Draft (awaiting user approval)
**Project:** `~/git/priv/claude-slack-bot`
**Drivers:** production failures in current pid-based / hook-polled design — see *Background*.

---

## 1. Background

The first iteration of `claude-slack-bot` shipped a single-actuator model:
- Hooks (SessionStart, UserPromptSubmit, Stop, Notification, SessionEnd) `POST` JSON to the daemon.
- One global actuator with a single `asyncio.Lock` serialised the three-call zellij sequence.
- The Stop hook scraped the CC transcript JSONL with a `sleep + tac + jq` loop.
- Liveness was inferred from a `cc_pid` recorded at SessionStart and re-checked at Slack-reply time.

In production this surfaced five concrete failures, all confirmed in the field over two days:

1. **Wrong-turn responses** — Stop hook fired before CC flushed the latest assistant message, so the bot posted the *previous* turn's response into Slack.
2. **`status='ended'` resurrection** — `refresh_liveness` un-ended rows that `_on_end` had just marked ended, producing nondeterministic recovery vs hijack.
3. **Two-chat freeze** — concurrent replies in two threads caused a focus race: actuator A's `focus-pane-id` was followed by actuator B's `focus-pane-id` *before* A's `write-chars`, so A's text typed into B's pane.
4. **Stale-pid false deaths** — `claude-auto-resume` restarts CC under the same `session_id` with a fresh pid; the recorded `cc_pid` was stale within seconds; Slack replies got rejected with "CC process no longer running" while CC was actively responding.
5. **Silent Socket Mode zombification** — after Slack's ~5h session rotation the new Bolt session occasionally stopped delivering events without raising, dropping ~2h of replies before manual restart. The in-process `is_ping_pong_failing` check used an undocumented private SDK method.

The independent reviewer also flagged:

- **`--resume <name>` substring match is wrong** — `/proc/PID/cmdline` is NUL-separated; the literal byte sequence `--resume Finance` (with a space) never appears in real CC cmdlines. The check matched only by accidental shell-script collisions and missed every real CC.
- **`/proc/*/cmdline` scan runs on the event loop** — blocking I/O, O(processes) on every Slack reply.
- **`claim_name` and `get_session_by_thread` are not transactional** — concurrent `/rn` races can produce inconsistent thread ownership.
- **`DeliveryDedupe.consume` does raw-text equality** — any whitespace normalisation by CC breaks the dedupe.

This spec replaces the affected components with a worker-per-conversation model whose lock scope is minimal, semantic correlation that is race-free by construction, and a daemon-side transcript reader that subsumes most of the existing hook surface.

---

## 2. Goals

- Eliminate the wrong-turn race by construction (no timing-based heuristics).
- Allow two concurrent Slack conversations to run without interleaved keystrokes.
- Treat CC liveness as derived from the *current* OS state, not from cached values that go stale on restart.
- Reduce hook surface to "metadata in, status out" — pull message content from CC's own transcript file.
- Keep the daemon recoverable when Slack's Socket Mode silently breaks.

## 3. Non-goals

- Cross-platform support beyond Linux + WSL2 (inotify is required).
- Multi-user / multi-host deployment. Daemon runs co-located with the panes it actuates.
- Replacing the SQLite registry, the systemd unit, the Slack app permissions model, or the auto-recovery semantics for resumed sessions.

---

## 4. Architecture (changes)

```
                  Slack workspace
                       │  Socket Mode
                       ▼
              ┌────────────────────┐
              │   Bolt receiver    │
              │   (asyncio task)   │
              └────────┬───────────┘
                       │  thread reply  ┌──────────────┐
                       └───────────────►│              │
                                        │  Supervisor  │
   Hooks (SessionStart/                 │              │
   SessionEnd/Notification)             └─────┬────────┘
        │  POST /event                        │ spawn/reap
        ▼                                     ▼
   ┌──────────────┐                  ┌──────────────────┐
   │ aiohttp /    │   enqueue        │ Worker[sid]      │
   │ event endpt  │─────────────────►│  asyncio.Queue   │
   └──────────────┘                  │  + asyncio.Task  │
                                     │  - posted_uuids  │
   Per-session inotify on            │  - prompt_uuid   │
   transcript JSONL                  └─────┬────────────┘
        │  new JSONL line                  │ deliver()
        ▼                                  ▼
   Transcript reader                ┌────────────────┐
   enqueues parsed events           │ Global         │
   into Worker[sid]                 │ ZellijActuator │
                                    │  asyncio.Lock  │
                                    └────────────────┘

   SQLite registry: single writer, durable state.
```

### 4.1 What is per-worker

One `Worker` per `cc_session_id`. Each worker owns:

| State | Type | Purpose |
|---|---|---|
| `queue` | `asyncio.Queue` | inbound events + inbound Slack replies, merged FIFO |
| `posted_uuids` | `set[str]` | message uuids already mirrored to Slack |
| `prompt_uuid` | `str \| None` | uuid of the latest user prompt (for `parentUuid` matching) |
| `task` | `asyncio.Task` | the loop draining the queue |
| `transcript_reader` | `TranscriptReader` | the inotify-driven reader for this session's JSONL |
| `last_activity` | `float` | monotonic timestamp; supervisor reaps after 5 min idle |

Each worker's loop is the only code that **calls Slack post APIs** and **calls the actuator** for its session. No cross-worker shared mutable state.

### 4.2 What is global

| Component | Reason |
|---|---|
| `Registry` (SQLite) | Single-writer durability is already correct |
| `ZellijActuator` (`asyncio.Lock`) | `zellij action focus-pane-id` is global state; the 3-call sequence must remain atomic across all workers |
| `SlackIO` (one `AsyncWebClient`) | Slack rate limiting + connection reuse |
| `Supervisor` | Worker lifecycle (spawn on first event, reap on idle) |
| `LivenessCache` | Shared 10s memoization of `/proc` scan results |
| `Bolt receiver` | Single Socket Mode connection serves all sessions |

---

## 5. Transcript reader (D+K)

### 5.1 What goes away

- `Stop` hook's transcript-scraping logic and the `sleep 0.4` retry.
- The `UserPromptSubmit` hook's prompt-text capture (transcript already records it).
- `DeliveryDedupe` (replaced by per-worker `posted_uuids` keyed on message uuid).

### 5.2 What replaces it

For each session the daemon registers, a `TranscriptReader`:

1. Opens an `inotify` watch on the JSONL file (Linux only; user is on WSL2/Linux).
2. Maintains a file-offset cursor so we read only new bytes after each `IN_MODIFY`.
3. Parses appended lines as JSON. For each entry:
   - `type == "user"` → enqueue `{kind:"prompt", uuid, parentUuid, text}` into the worker.
   - `type == "assistant"` with text content → enqueue `{kind:"response", uuid, parentUuid, text}`.
   - other types (tool_use, file-history-snapshot, hook log) → ignored for v1.
4. Tolerates partial lines at the cursor (buffer until the next newline).
5. Handles truncation / log rotation defensively: if the file's inode changes or shrinks, re-open from offset 0.

The worker mirrors **every** assistant message it receives, dedup'd by `uuid`. Because the reader only emits messages already flushed to disk, the "wrong turn" race is gone by construction.

### 5.3 Stop and UserPromptSubmit hooks

Both become **optional** in the new flow. Reasons to keep them:

- `UserPromptSubmit` is useful for the Slack-reply echo suppression: when a delivery types text into a pane, the resulting user-message in the transcript would be mirrored back to Slack. We suppress that by recording `(sid, text)` in the worker's `pending_delivery_echo` set when the delivery happens and consuming it when the matching transcript line arrives. (Replaces the broken whitespace-equality `DeliveryDedupe`.)
- `Stop` is useful as a "turn complete" signal for future features (e.g. tool-call summaries, status indicators). v1 does not require it.

The other two hooks remain mandatory:

- `SessionStart` — provides metadata (cwd, zellij_session, zellij_pane_id, agent, transcript_path) and triggers `Supervisor.start_session(sid)`.
- `SessionEnd` — triggers `Supervisor.stop_session(sid)` which cancels the transcript watch and reaps the worker after the queue drains.
- `Notification` — permission-prompt mirroring; not derivable from transcript.

---

## 6. Liveness

### 6.1 Authority

`status` in the registry becomes **diagnostic only**. The reply path always asks `LivenessCache.is_alive(sid, name, cc_pid)` regardless of status. Auto-recovery at SessionStart asks the same function for prior rows. No other gate.

### 6.2 The check itself

```python
def session_is_alive(cc_session_id, cc_pid, name):
    # 1. Look for the session_id UUID as a standalone argv token in any /proc/*/cmdline.
    if cc_session_id and _argv_contains(cc_session_id):
        return True
    # 2. Look for `--resume <name>` as adjacent argv tokens.
    if name and _argv_pair_contains("--resume", name):
        return True
    # 3. Fall back to recorded pid.
    if cc_pid is not None and cc_pid > 0:
        return _pid_alive(cc_pid)
    return True  # no info → don't refuse delivery
```

Where `_argv_contains` and `_argv_pair_contains` parse `/proc/PID/cmdline` by splitting on `\x00` and compare argv tokens **exactly**. No substring matching.

### 6.3 Caching + threading

`LivenessCache.is_alive` wraps `session_is_alive` with:

- A `(sid, name, cc_pid) → (timestamp, bool)` dict, TTL **10 seconds**.
- The actual `/proc` scan runs in `asyncio.to_thread` so the event loop stays unblocked.

### 6.4 Reaper

A background `asyncio` task runs every 5 minutes:

- Scans all rows with `status='active'`.
- For each, runs `LivenessCache.is_alive`.
- If dead, `set_status('ended')`. Does NOT delete the row — auto-recovery still wants to inherit the name+thread on next CC.

---

## 7. Auto-recovery and registration

### 7.1 Delete time-based staleness

`Registry.find_recoverable_session` currently uses `last_event_at < cutoff`. **Replaced**: the daemon checks `session_is_alive` on each candidate row and only inherits when the candidate is dead. One source of truth.

### 7.2 Auto-register on any event

Today only `SessionStart` upserts a row. Other events for an unknown `sid` log a warning and drop. **Replaced**: `EventHandlers.handle` upserts (creating if absent) for any event that carries the required metadata. The path is the same as SessionStart, including the auto-recovery step.

### 7.3 Transactional name claim

`Registry.claim_name` wraps the existing read+update sequence in a single `BEGIN IMMEDIATE / COMMIT` so a concurrent claim cannot end up with two rows holding the same name. The SQLite connection is opened with `isolation_level=None` so we manage transactions explicitly.

---

## 8. Watchdogs (defence in depth)

### 8.1 In-process

Loop every 30s, awaits `socket_handler.client.is_connected()`. If false **or** if no event has been received from Slack for `INACTIVITY_THRESHOLD` (90s), force a disconnect+reconnect. Drops the `is_ping_pong_failing` private-API dependency.

### 8.2 systemd

`claude-slack-bot.service` gains `WatchdogSec=600`. Daemon calls `sd_notify("WATCHDOG=1")` on:
- Every 60s heartbeat (background task).
- Every successful Slack post (best-effort, errors swallowed).

If the daemon hangs for 10 min, systemd kills and restarts it. Registry state is durable; workers re-establish on next event.

---

## 9. Hook contract (final)

Three hooks remain. All three send minimal JSON; all parsing happens daemon-side.

### `session_start.sh`
```json
{
  "v": 1,
  "kind": "start",
  "session_id": "<UUID>",
  "agent": "claude" | "codex" | "gemini",
  "cwd": "<abs path>",
  "zellij_session": "<env or empty>",
  "zellij_pane_id": "<env or empty>",
  "cc_pid": <PPID>,
  "transcript_path": "<from stdin>"
}
```

### `session_end.sh`
```json
{
  "v": 1,
  "kind": "end",
  "session_id": "<UUID>",
  "reason": "<from stdin or 'unknown'>"
}
```

### `notify.sh`
```json
{
  "v": 1,
  "kind": "notification",
  "session_id": "<UUID>",
  "agent": "<env>",
  "message": "<from stdin>",
  "tool_request": "<extracted from transcript, optional>",
  "context": "<extracted, optional>",
  "zellij_session": "<env>",
  "zellij_pane_id": "<env>",
  "cc_pid": <PPID>
}
```

`prompt.sh` and `stop.sh` are kept on disk for the rename-blocking path (codex `/rn` detection) but no longer carry response/prompt text — the transcript reader is the source.

---

## 10. Event flow examples

### 10.1 Happy path: CC produces a response

1. User types prompt in CC.
2. CC writes user JSONL entry → inotify fires → reader emits `prompt` event into worker.
3. Worker stores `prompt_uuid`; if Slack-echo-suppression has a matching `pending_delivery`, the prompt is **not** mirrored. Otherwise, the worker posts `👤 <text>` into the Slack thread.
4. CC writes assistant JSONL entry → reader emits `response` event with `uuid` and `parentUuid=prompt_uuid`.
5. Worker: `uuid not in posted_uuids` → post `🤖 <text>` to Slack thread, add uuid to `posted_uuids`.

### 10.2 Slack reply → pane

1. Bolt receives thread reply, looks up worker by `thread_ts` → registry → sid.
2. Enqueues `{kind:"slack_reply", text, msg_ts}` into Worker[sid].
3. Worker:
   - `LivenessCache.is_alive(sid, name, cc_pid)` → True.
   - Records `pending_delivery = text` so the UserPromptSubmit-driven prompt event is suppressed.
   - Calls `actuator.deliver(zellij_session, pane_id, text)` (which acquires the global lock for the 3-call zellij sequence).
   - Reacts ✅ on the Slack message.
4. Reader sees the user JSONL line caused by the keystrokes, emits `prompt`, worker consumes the `pending_delivery` and skips the mirror.

### 10.3 CC restart via claude-auto-resume

1. Old CC exits; `SessionEnd` may or may not fire (the hook is best-effort).
2. New CC starts with `--resume <name>`, fires `SessionStart` with new `cc_pid`, same `session_id`.
3. Supervisor: worker for this sid already exists (or is reaped). On `start` event, registry upserts new `cc_pid` and `zellij_pane_id`; auto-recovery is a no-op because the row already has its name. The TranscriptReader rebinds inotify (same path, same inode usually).
4. Slack replies continue to deliver. `LivenessCache.is_alive` finds `--resume <name>` in the new cmdline.

### 10.4 Two concurrent Slack replies (one per session)

1. Bolt enqueues reply A into Worker[A], reply B into Worker[B].
2. Both workers run `actuator.deliver` concurrently. The actuator's `asyncio.Lock` serialises the 3-call zellij sequence: A's `focus → write → enter` completes before B's begins (~50–150 ms total per delivery).
3. Each worker independently reacts ✅ on its own Slack message.

No focus race; no head-of-line blocking on Slack posting.

---

## 11. Migration

The redesign is one daemon, not a wire-protocol change. Plan:

1. Land the new modules (`worker.py`, `supervisor.py`, `transcript_reader.py`, `liveness_cache.py`, `sd_notify.py`) alongside existing code.
2. Switch `__main__.py` to the new wiring behind a feature flag (`SLACKBOT_WORKER_MODE=1`) so the legacy path is reachable if something regresses.
3. Update hooks (`session_start.sh` adds `transcript_path`; `prompt.sh` / `stop.sh` keep working but their text fields are unused).
4. Run for one day with the flag on; verify two-chat concurrency, Stop-race-free turns, idle 24h.
5. Delete legacy paths (`DeliveryDedupe`, hook-side transcript polling, status-flip in `refresh_liveness`, time-based stale in `find_recoverable_session`).

---

## 12. Test strategy

- **Unit:** `TranscriptReader` against synthetic JSONL fixtures (partial line, file rotation, malformed JSON skip). `LivenessCache` with monotonic clock injection. `Worker` with fake actuator + slack + transcript stream. `Registry.claim_name` under simulated concurrency.
- **Integration:** spin the daemon against a fake Slack client and a real /tmp transcript file; drive a full happy-path turn; assert (a) prompt mirrored, (b) response mirrored, (c) no duplicate, (d) Slack reply types into a tmp file used as a fake pane.
- **Two-chat concurrency test:** two workers, two delivery calls scheduled together, assert the actuator log shows two contiguous 3-call sequences (no interleave).
- **Liveness cache test:** verify scan runs ≤ 1× per 10s for the same key under burst of 100 lookups.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| inotify is Linux-only | Documented requirement; user is on Linux/WSL2 |
| Transcript JSONL format changes between CC versions | Reader is defensive: skips lines it can't parse, logs WARN, never crashes |
| Per-worker memory grows unbounded if `posted_uuids` is unbounded | LRU-cap `posted_uuids` to last 1000 entries per worker |
| Echo suppression false negatives if CC normalises whitespace | Suppress by uuid (transcript-side), not by raw text — `pending_delivery` becomes a mapping by uuid resolved when the user JSONL line lands |
| Reaper deletes a row mid-flight | Reaper only marks `status='ended'`; it never `DELETE`s. Auto-recovery still finds the row by name+cwd. |

---

## 14. Out of scope

- A "/cc-list" / "/cc-mute" / "/cc-status" Slack slash-command surface.
- Replaying historical transcripts to a fresh Slack thread.
- File-attachment support either direction.
- Per-tool-call mirroring (could be added later by extending the transcript reader to emit `tool_use` events).

---

## 15. Deliverables

1. New modules: `src/slackbot/{worker,supervisor,transcript_reader,liveness_cache,sd_notify}.py`.
2. Refactored modules: `__main__.py`, `handlers.py`, `reply_router.py`, `registry.py` (transactional claim_name, drop status-flip), `process_liveness.py` (argv-token matcher), `zellij_io.py` (unchanged; lock stays).
3. Updated hooks: `session_start.sh` (sends transcript_path), `notify.sh` (cc_pid kept), others trimmed.
4. systemd unit: add `WatchdogSec=600`, `Type=notify`.
5. Tests covering the cases in §12.
6. Migration notes appended to `README.md`.
