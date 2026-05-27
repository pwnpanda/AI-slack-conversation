import os
import socket
from pathlib import Path

import pytest

from slackbot.sd_notify import notify, ready, watchdog


def test_noop_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # Must not raise even though there's nowhere to send.
    notify("WATCHDOG=1")
    ready()
    watchdog()


def test_sends_message_to_unix_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "notify.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    sock.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
    notify("WATCHDOG=1")
    data, _ = sock.recvfrom(1024)
    assert data == b"WATCHDOG=1"
    sock.close()
    os.unlink(sock_path)
