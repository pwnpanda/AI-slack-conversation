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
from slackbot.handlers import EventHandlers
from slackbot.logging_setup import configure as configure_logging
from slackbot.registry import Registry
from slackbot.reply_router import ReplyRouter
from slackbot.server import make_app
from slackbot.slack_io import SlackIO
from slackbot.zellij_io import ZellijActuator

log = logging.getLogger("slackbot.main")


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
    slack_io = SlackIO(web_client, cfg.slack_channel_id)
    handlers = EventHandlers(reg, slack_io)
    actuator = ZellijActuator()
    router = ReplyRouter(reg, actuator, slack_io)

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
        await router.on_reply(thread_ts=thread_ts, text=text, msg_ts=msg_ts)

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

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        socket_task.cancel()
        await http_runner.cleanup()
        reg.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
