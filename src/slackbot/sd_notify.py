"""Thin sd_notify wrapper. No external deps — Unix datagram socket only."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(message: str) -> None:
    """Send a single sd_notify message. Silent no-op when NOTIFY_SOCKET unset."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    # systemd uses an abstract socket if the path starts with '@'.
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode("utf-8"), sock_path)
    except OSError as exc:
        log.warning("sd_notify failed: %s", exc)


def ready() -> None:
    notify("READY=1")


def watchdog() -> None:
    notify("WATCHDOG=1")
