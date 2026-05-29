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


@pytest.mark.asyncio
async def test_concurrent_deliveries_do_not_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent deliver() calls must produce two complete sequences in the
    log, not interleaved — i.e. all three calls for delivery A finish before any
    of delivery B's calls run (or vice versa)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    # Slow zellij so the race window is wide.
    script = bin_dir / "zellij"
    script.write_text(
        f"""#!/usr/bin/env bash
echo "$@" >> {log_file}
sleep 0.05
exit 0
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    act = ZellijActuator()
    import asyncio as _aio

    await _aio.gather(
        act.deliver(session="main", pane_id="A", text="AAA"),
        act.deliver(session="main", pane_id="B", text="BBB"),
    )

    lines = log_file.read_text().splitlines()
    assert len(lines) == 6
    # The three calls for whichever delivery ran first should be contiguous,
    # i.e. the lock keeps the sequence atomic. Find which pane went first.
    first_pane = "A" if "focus-pane-id A" in lines[0] else "B"
    other_pane = "B" if first_pane == "A" else "A"
    first_text = "AAA" if first_pane == "A" else "BBB"
    other_text = "BBB" if first_pane == "A" else "AAA"
    assert f"focus-pane-id {first_pane}" in lines[0]
    assert f"write-chars {first_text}" in lines[1]
    assert "write 13" in lines[2]
    assert f"focus-pane-id {other_pane}" in lines[3]
    assert f"write-chars {other_text}" in lines[4]
    assert "write 13" in lines[5]


@pytest.mark.asyncio
async def test_spawn_pane_with_command_runs_new_pane_then_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    _make_fake_zellij(bin_dir, log_file=log_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    act = ZellijActuator()
    await act.spawn_pane_with_command(
        session="main",
        command_argv=("claude", "--dangerously-skip-permissions"),
        initial_text="/rn kbd",
        delay_seconds=0.01,
    )
    calls = log_file.read_text().splitlines()
    assert len(calls) == 3
    assert "action new-pane -- claude --dangerously-skip-permissions" in calls[0]
    assert "action write-chars /rn kbd" in calls[1]
    assert "action write 13" in calls[2]


@pytest.mark.asyncio
async def test_spawn_pane_rejects_empty_argv() -> None:
    act = ZellijActuator()
    with pytest.raises(ZellijError):
        await act.spawn_pane_with_command(
            session="main", command_argv=(), initial_text="/rn x", delay_seconds=0.0
        )
