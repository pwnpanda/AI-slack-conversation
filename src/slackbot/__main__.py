"""Daemon entry point. Wires supervisor, transcript readers, watchdogs."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from nio import AsyncClient, ReactionEvent, RoomMessageText, SyncResponse

from slackbot import sd_notify
from slackbot.config import load_config
from slackbot.deletion import delete_thread
from slackbot.handlers import EventHandlers
from slackbot.logging_setup import configure as configure_logging
from slackbot.matrix_commands import MatrixCommandHandler
from slackbot.matrix_io import MatrixIO
from slackbot.matrix_rooms import ensure_joined, resolve_host_room
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.server import make_app
from slackbot.supervisor import Supervisor
from slackbot.zellij_io import ZellijActuator

log = logging.getLogger("slackbot.main")

_READER_POLL_INTERVAL = 0.5
_REAPER_INTERVAL = 30.0
_WATCHDOG_INTERVAL = 60.0
# Reacting with 🗑️ on a thread's top-level deletes the thread. Accept both
# the emoji-presentation (VS16) and plain codepoint forms clients may send.
_DELETE_EMOJI = {"\U0001f5d1️", "\U0001f5d1"}
# Registry key holding the Matrix sync cursor between daemon runs.
_SYNC_TOKEN_KEY = "matrix_sync_token"


async def reader_pump(supervisor: Supervisor) -> None:
    while True:
        try:
            await supervisor.pump_readers()
        except Exception:
            log.exception("reader pump iteration failed")
        await asyncio.sleep(_READER_POLL_INTERVAL)


async def reaper(supervisor: Supervisor) -> None:
    while True:
        try:
            await supervisor.reap_once()
        except Exception:
            log.exception("reaper iteration failed")
        await asyncio.sleep(_REAPER_INTERVAL)


async def watchdog_heartbeat() -> None:
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL)
        sd_notify.watchdog()


async def amain() -> None:
    cfg = load_config()
    configure_logging(cfg.log_level)
    log.info(
        "starting claude-slack-bot port=%d default_room=%s",
        cfg.port,
        cfg.matrix_default_room,
    )

    reg = Registry(cfg.db_path)
    reg.open()

    client = AsyncClient(
        cfg.matrix_homeserver,
        cfg.matrix_user_id,
        device_id=cfg.matrix_device_id,
    )
    # A single-user bot does not need libolm/store state for v1 — we just
    # set the access token directly and let sync_forever handle reconnects.
    client.access_token = cfg.matrix_access_token
    client.user_id = cfg.matrix_user_id
    client.device_id = cfg.matrix_device_id

    # Optional second client logged in as the human, used for posting CC-typed
    # prompts under the user's identity. When configured, the bot will keep
    # two access tokens active and route 👤 mirrors via this client.
    user_client = None
    if cfg.matrix_user_user_id and cfg.matrix_user_access_token:
        user_client = AsyncClient(cfg.matrix_homeserver, cfg.matrix_user_user_id)
        user_client.access_token = cfg.matrix_user_access_token
        user_client.user_id = cfg.matrix_user_user_id
        log.info("user-account mirroring enabled (puppet user=%s)", cfg.matrix_user_user_id)

    # Dedupe by Matrix event_id so a message delivered via the on_room_message
    # callback (and potentially also via initial sync replay) only gets
    # actuated once. Also used as a self-loop guard for the user-account
    # puppet: when the bot posts as the human via the second client, the
    # @ai-bot sync still sees that event in the room timeline; without
    # this set, it'd route the bot's own post to the actuator and the
    # text would loop forever into the pane.
    delivered_event_ids: set[str] = set()
    _DEDUPE_CAP = 4096

    def _remember_self_post(event_id: str) -> None:
        delivered_event_ids.add(event_id)
        if len(delivered_event_ids) > _DEDUPE_CAP:
            for x in list(delivered_event_ids)[: _DEDUPE_CAP // 2]:
                delivered_event_ids.discard(x)

    # Per-host routing: resolve (or create) a room named after this machine's
    # hostname and send every provider there, so work/private separate by
    # host. Falls back to the static default + per-agent rooms when disabled.
    default_room = cfg.matrix_default_room
    agent_rooms = cfg.agent_rooms
    if cfg.room_by_hostname:
        default_room = await resolve_host_room(
            cfg.matrix_homeserver,
            cfg.matrix_access_token,
            cfg.hostname,
            cfg.matrix_user_user_id,
        )
        agent_rooms = {}  # all providers share the host room
        log.info("per-host routing: %s -> %s", cfg.hostname, default_room)
        # The puppet human must be a member to post prompt mirrors.
        if cfg.matrix_user_access_token:
            await ensure_joined(cfg.matrix_homeserver, cfg.matrix_user_access_token, default_room)

    matrix_io = MatrixIO(
        client,
        default_room,
        agent_rooms,
        user_client=user_client,
        on_self_post=_remember_self_post,
    )
    actuator = ZellijActuator()
    supervisor = Supervisor(reg=reg, matrix=matrix_io, actuator=actuator)
    handlers = EventHandlers(reg, supervisor, matrix_io)
    router = ReplyRouter(reg=reg, supervisor=supervisor, matrix=matrix_io)
    commands = MatrixCommandHandler(
        reg=reg,
        matrix=matrix_io,
        actuator=actuator,
        zellij_session=cfg.new_pane_zellij_session,
        new_pane_command=cfg.new_pane_command,
        new_pane_delay_seconds=cfg.new_pane_delay_seconds,
    )

    loop = asyncio.get_running_loop()

    # Resume the sync cursor the previous daemon stored, so a restart neither
    # replays room history nor drops replies sent while we were down. This
    # matters because prompt mirrors are posted through the user-puppet
    # account: they arrive back as the *human's* messages, so the
    # `event.sender == client.user_id` guard below does not filter them, and
    # `delivered_event_ids` starts empty on every restart. Without a cursor,
    # the first sync returns the whole visible timeline and every past reply
    # (ours included) gets retyped into the pane.
    stored_sync_token = reg.get_meta(_SYNC_TOKEN_KEY)
    if stored_sync_token:
        client.next_batch = stored_sync_token
    # With no stored cursor there is nothing to resume from, so the first sync
    # is history rather than new traffic. nio runs event callbacks while
    # handling the sync and response callbacks afterwards, so this flag flips
    # only once that first batch has been skipped.
    backlog_consumed = bool(stored_sync_token)

    async def handle_thread_reply(room_id: str, thread_root: str, text: str, event_id: str) -> None:
        if event_id in delivered_event_ids:
            return
        _remember_self_post(event_id)
        if text.strip().lower() in ("/del", "/delete"):
            await delete_thread(matrix_io, reg, room_id, thread_root)
            return
        log.info(
            "thread reply received: room=%s thread_root=%s event_id=%s len=%d",
            room_id,
            thread_root,
            event_id,
            len(text),
        )
        await router.on_reply(room_id=room_id, thread_root=thread_root, text=text, msg_ts=event_id)

    async def on_room_message(room, event) -> None:
        sd_notify.watchdog()
        if not backlog_consumed:
            return  # first-ever sync: room history, not traffic to act on
        if event.sender == client.user_id:
            return  # @ai-bot's own post echoed back by sync
        if event.event_id in delivered_event_ids:
            return  # something we posted (e.g. as the user puppet) coming back via sync
        content = event.source.get("content", {}) if hasattr(event, "source") else {}
        relates = content.get("m.relates_to") or {}
        text = getattr(event, "body", "") or content.get("body", "")
        # Top-level (non-threaded) messages may carry slash commands.
        if relates.get("rel_type") != "m.thread":
            await commands.maybe_handle(room_id=room.room_id, text=text, msg_ts=event.event_id)
            return
        thread_root = relates.get("event_id")
        if not thread_root:
            return
        await handle_thread_reply(room.room_id, thread_root, text, event.event_id)

    async def on_reaction(room, event) -> None:
        # 🗑️ on a thread's top-level message → delete the whole thread.
        sd_notify.watchdog()
        if not backlog_consumed:
            return
        if event.sender == client.user_id:
            return
        relates = event.source.get("content", {}).get("m.relates_to", {}) or {}
        if relates.get("rel_type") != "m.annotation":
            return
        if relates.get("key") not in _DELETE_EMOJI:
            return
        target = relates.get("event_id")
        if not target:
            return
        sess = reg.get_session_by_matrix_thread(target, room.room_id)
        if sess is None:
            return  # reaction on some non-thread-root event; ignore
        await delete_thread(matrix_io, reg, room.room_id, target)

    def _on_sync(response) -> None:
        nonlocal backlog_consumed
        sd_notify.watchdog()
        backlog_consumed = True
        if getattr(response, "next_batch", None):
            reg.set_meta(_SYNC_TOKEN_KEY, response.next_batch)

    client.add_event_callback(on_room_message, RoomMessageText)
    client.add_event_callback(on_reaction, ReactionEvent)
    client.add_response_callback(_on_sync, SyncResponse)

    http_app = make_app(handlers)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    http_site = web.TCPSite(http_runner, "127.0.0.1", cfg.port)
    await http_site.start()
    log.info("http event endpoint listening on 127.0.0.1:%d", cfg.port)

    # Re-attach transcript readers for sessions that the previous daemon instance
    # was already watching. Without this, a daemon restart would leave existing CC
    # sessions un-mirrored until they fire SessionStart again (which only happens
    # when CC itself restarts).
    rows = reg.list_active_with_transcript()
    for row in rows:
        if row.transcript_path:
            start_offset = reg.get_transcript_offset(row.cc_session_id)
            supervisor.attach_reader(
                row.cc_session_id, row.transcript_path, start_offset=start_offset
            )
            await supervisor.get_or_create(row.cc_session_id)
    if rows:
        log.info("re-attached %d transcript readers on startup", len(rows))

    sd_notify.ready()

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    tasks: list[asyncio.Task] = []
    # sync_forever does its own retry on transient errors; HTTP failures bubble
    # up and crash the task, which is the desired behaviour — Restart=on-failure
    # gives us a clean restart with no lingering state.
    tasks.append(asyncio.create_task(client.sync_forever(timeout=30000, full_state=False)))
    tasks.append(asyncio.create_task(reader_pump(supervisor)))
    tasks.append(asyncio.create_task(reaper(supervisor)))
    tasks.append(asyncio.create_task(watchdog_heartbeat()))

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await supervisor.shutdown()
        await client.close()
        if user_client is not None:
            await user_client.close()
        await http_runner.cleanup()
        reg.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
