"""Daemon entry point. Runs aiohttp event server + Slack Socket Mode together."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from slackbot.config import load_config
from slackbot.dedupe import DeliveryDedupe
from slackbot.handlers import EventHandlers
from slackbot.logging_setup import configure as configure_logging
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.server import make_app
from slackbot.slack_io import SlackIO
from slackbot.zellij_io import ZellijActuator

log = logging.getLogger("slackbot.main")


async def socket_health_watchdog(
    socket_handler: AsyncSocketModeHandler, interval_seconds: int = 30
) -> None:
    """Detect silently-broken Socket Mode sessions and force a reconnect.

    Slack rotates Socket Mode sessions every ~5h. After rotation the new session
    occasionally stops delivering events without raising any error — Bolt stays
    in a zombie state. `is_ping_pong_failing` is the SDK's native liveness probe;
    we poll it and reconnect when it goes True.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            client = socket_handler.client
            if client is None or not client.is_connected():
                continue
            if await client.is_ping_pong_failing():
                log.warning("Socket Mode ping/pong failing — forcing reconnect.")
                try:
                    await client.disconnect()
                except Exception:
                    log.exception("disconnect() raised; continuing to reconnect")
                await asyncio.sleep(1)
                try:
                    await client.connect()
                    log.info("Socket Mode reconnected by watchdog.")
                except Exception:
                    log.exception("reconnect() failed; watchdog will retry on next tick")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watchdog loop iteration failed; continuing")


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
    dedupe = DeliveryDedupe()
    handlers = EventHandlers(
        reg, slack_io, dedupe=dedupe, stale_after_seconds=cfg.stale_after_seconds
    )
    actuator = ZellijActuator()
    router = ReplyRouter(
        reg, actuator, slack_io, dedupe=dedupe, stale_after_seconds=cfg.stale_after_seconds
    )

    bolt = AsyncApp(token=cfg.slack_bot_token, client=web_client)

    @bolt.event("message")
    async def on_message(event, logger):
        if event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return
        text = event.get("text", "")
        msg_ts = event.get("ts", "")
        channel = event.get("channel", "")
        # INFO-level so we can verify in journalctl that messages are arriving
        # even when LOG_LEVEL=INFO (previously only visible at DEBUG).
        log.info(
            "thread reply received: channel=%s thread_ts=%s msg_ts=%s len=%d",
            channel,
            thread_ts,
            msg_ts,
            len(text),
        )
        await router.on_reply(channel=channel, thread_ts=thread_ts, text=text, msg_ts=msg_ts)

    socket_handler = AsyncSocketModeHandler(bolt, cfg.slack_app_token)
    http_app = make_app(handlers)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    http_site = web.TCPSite(http_runner, "127.0.0.1", cfg.port)
    await http_site.start()
    log.info("http event endpoint listening on 127.0.0.1:%d", cfg.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    socket_task = asyncio.create_task(socket_handler.start_async())
    watchdog_task = asyncio.create_task(socket_health_watchdog(socket_handler))

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        watchdog_task.cancel()
        socket_task.cancel()
        await http_runner.cleanup()
        reg.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
