"""Daemon entry point. Wires supervisor, transcript readers, watchdogs."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from slackbot import sd_notify
from slackbot.config import load_config
from slackbot.handlers import EventHandlers
from slackbot.liveness_cache import LivenessCache
from slackbot.logging_setup import configure as configure_logging
from slackbot.process_liveness import session_is_alive
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.server import make_app
from slackbot.slack_io import SlackIO
from slackbot.slack_poller import SlackPoller
from slackbot.supervisor import Supervisor
from slackbot.zellij_io import ZellijActuator

log = logging.getLogger("slackbot.main")

_READER_POLL_INTERVAL = 0.5
_REAPER_INTERVAL = 30.0
_WATCHDOG_INTERVAL = 60.0
_PING_CHECK_INTERVAL = 60.0


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


async def socket_health_watchdog(socket_handler: AsyncSocketModeHandler) -> None:
    """Force a Socket Mode reconnect only on positive evidence of a stuck socket.

    `is_ping_pong_failing()` is the SDK's own liveness signal: PING/PONG flow
    every ~10s between client and Slack independently of user activity. If it
    stops flowing, the socket is dead. Mere absence of message events is NOT
    evidence of breakage — quiet conversations are normal.
    """
    while True:
        await asyncio.sleep(_PING_CHECK_INTERVAL)
        client = socket_handler.client
        if client is None:
            continue
        try:
            if not await client.is_connected():
                continue  # Bolt's own reconnect logic handles disconnected sockets
            if await client.is_ping_pong_failing():
                log.warning("Socket Mode ping/pong failing — forcing reconnect")
                await client.disconnect()
                await asyncio.sleep(1)
                await client.connect()
        except Exception:
            log.exception("watchdog check failed")


async def amain() -> None:
    cfg = load_config()
    configure_logging(cfg.log_level)
    log.info(
        "starting claude-slack-bot port=%d channel=%s",
        cfg.port,
        cfg.slack_channel_id,
    )

    reg = Registry(cfg.db_path)
    reg.open()

    web_client = AsyncWebClient(token=cfg.slack_bot_token)
    slack_io = SlackIO(web_client, cfg.slack_channel_id, cfg.agent_channels)
    actuator = ZellijActuator()
    supervisor = Supervisor(reg=reg, slack=slack_io, actuator=actuator)
    liveness = LivenessCache(session_is_alive)
    handlers = EventHandlers(reg, supervisor, slack_io)
    router = ReplyRouter(reg=reg, supervisor=supervisor, liveness=liveness, slack=slack_io)

    bolt = AsyncApp(token=cfg.slack_bot_token, client=web_client)
    loop = asyncio.get_running_loop()

    # Bolt's on_message AND the poller both call this. Dedupe by Slack's msg_ts
    # so a message delivered via both paths only gets actuated once.
    delivered_msg_ts: set[str] = set()
    _DEDUPE_CAP = 4096

    async def handle_thread_reply(channel: str, thread_ts: str, text: str, msg_ts: str) -> None:
        if msg_ts in delivered_msg_ts:
            return
        delivered_msg_ts.add(msg_ts)
        # Bounded LRU-ish: drop arbitrary half when cap exceeded.
        if len(delivered_msg_ts) > _DEDUPE_CAP:
            for x in list(delivered_msg_ts)[: _DEDUPE_CAP // 2]:
                delivered_msg_ts.discard(x)
        log.info(
            "thread reply received: channel=%s thread_ts=%s msg_ts=%s len=%d",
            channel,
            thread_ts,
            msg_ts,
            len(text),
        )
        await router.on_reply(channel=channel, thread_ts=thread_ts, text=text, msg_ts=msg_ts)

    @bolt.event("message")
    async def on_message(event, logger):
        sd_notify.watchdog()
        if event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        text = event.get("text", "")
        msg_ts = event.get("ts", "")
        channel = event.get("channel", "")
        await handle_thread_reply(channel, thread_ts, text, msg_ts)

    socket_handler = AsyncSocketModeHandler(bolt, cfg.slack_app_token)
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
            supervisor.attach_reader(row.cc_session_id, row.transcript_path)
            await supervisor.get_or_create(row.cc_session_id)
    if rows:
        log.info("re-attached %d transcript readers on startup", len(rows))

    sd_notify.ready()

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(socket_handler.start_async()))
    tasks.append(asyncio.create_task(reader_pump(supervisor)))
    tasks.append(asyncio.create_task(reaper(supervisor)))
    tasks.append(asyncio.create_task(watchdog_heartbeat()))
    tasks.append(asyncio.create_task(socket_health_watchdog(socket_handler)))
    poller = SlackPoller(
        reg=reg,
        client=web_client,
        deliver=handle_thread_reply,
        interval_seconds=15.0,
    )
    tasks.append(asyncio.create_task(poller.run()))

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await supervisor.shutdown()
        await http_runner.cleanup()
        reg.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
