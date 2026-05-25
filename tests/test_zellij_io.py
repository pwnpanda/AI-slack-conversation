import os
import stat
from pathlib import Path

import pytest

from slackbot.zellij_io import ZellijActuator, ZellijError


def _make_fake_zellij(bin_dir: Path, *, exit_code: int = 0, log_file: Path | None = None) -> None:
    script = bin_dir / "zellij"
    log_path = str(log_file) if log_file else "/dev/null"
    script.write_text(
        f"""#!/usr/bin/env bash
echo "$@" >> {log_path}
exit {exit_code}
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.asyncio
async def test_deliver_calls_focus_then_write_then_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    _make_fake_zellij(bin_dir, log_file=log_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    act = ZellijActuator()
    await act.deliver(session="main", pane_id="0", text="hello world")

    calls = log_file.read_text().splitlines()
    assert len(calls) == 3
    assert "--session main action focus-pane-id 0" in calls[0]
    assert "--session main action write-chars hello world" in calls[1]
    assert "--session main action write 13" in calls[2]


@pytest.mark.asyncio
async def test_deliver_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_zellij(bin_dir, exit_code=2)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    act = ZellijActuator()
    with pytest.raises(ZellijError):
        await act.deliver(session="main", pane_id="0", text="hi")


@pytest.mark.asyncio
async def test_deliver_tolerates_already_focused_on_focus_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    script = bin_dir / "zellij"
    script.write_text(
        f"""#!/usr/bin/env bash
echo "$@" >> {log_file}
if [[ "$*" == *"focus-pane-id"* ]]; then
  echo "Pane Terminal(0) is already focused" >&2
  exit 2
fi
exit 0
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    act = ZellijActuator()
    await act.deliver(session="main", pane_id="0", text="hi")
    calls = log_file.read_text().splitlines()
    assert len(calls) == 3  # focus tolerated, then write-chars + Enter
