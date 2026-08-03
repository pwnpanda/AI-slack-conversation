import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stub_curl(tmp_path: Path) -> tuple[Path, Path]:
    """Install a curl stub on PATH that records every posted -d payload.

    `jq -n` emits pretty-printed multi-line JSON, so each payload goes into its
    own file rather than one line of a shared log.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    capture = tmp_path / "payloads"
    capture.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-d" ]; then\n'
        '    printf "%s" "$a" > "$CAPTURE/$(ls "$CAPTURE" | wc -l).json"\n'
        "  fi\n"
        '  prev="$a"\n'
        "done\n"
        "exit 0\n"
    )
    curl.chmod(0o755)
    return bin_dir, capture


def _run_hook(script: str, payload: dict, tmp_path: Path, agent: str = "claude"):
    bin_dir, capture = _stub_curl(tmp_path)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / script)],
        input=json.dumps(payload),
        env={
            "PATH": f"{bin_dir}:/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin",
            "SLACKBOT_AGENT": agent,
            "SLACKBOT_PORT": "1",
            "CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    posted = [json.loads(f.read_text()) for f in sorted(capture.iterdir())]
    return result, posted


def _run_prompt_hook(agent: str, prompt: str, tmp_path: Path) -> subprocess.CompletedProcess:
    result, _ = _run_hook("prompt.sh", {"session_id": "s1", "prompt": prompt}, tmp_path, agent)
    return result


def test_codex_rename_prompt_blocks_model_submission(tmp_path: Path) -> None:
    result = _run_prompt_hook("codex", "rn named-session", tmp_path)

    assert result.returncode == 2
    assert result.stdout == "{}\n"
    assert "Renamed Matrix thread to named-session" in result.stderr


def test_claude_rename_prompt_does_not_block(tmp_path: Path) -> None:
    result = _run_prompt_hook("claude", "rn named-session", tmp_path)

    assert result.returncode == 0
    assert result.stdout == "{}\n"


def test_prompt_hook_forwards_transcript_path(tmp_path: Path) -> None:
    """Without transcript_path the daemon cannot tell that a transcript reader
    already mirrors this session, and mirrors the hook text a second time."""
    result, posted = _run_hook(
        "prompt.sh",
        {"session_id": "s1", "prompt": "hello", "cwd": "/p", "transcript_path": "/p/t.jsonl"},
        tmp_path,
    )

    assert result.returncode == 0
    prompts = [p for p in posted if p["kind"] == "prompt"]
    assert len(prompts) == 1
    assert prompts[0]["transcript_path"] == "/p/t.jsonl"


def test_stop_hook_forwards_transcript_path(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    result, posted = _run_hook(
        "stop.sh",
        {
            "session_id": "s1",
            "last_assistant_message": "done",
            "transcript_path": str(transcript),
        },
        tmp_path,
    )

    assert result.returncode == 0
    assert len(posted) == 1
    assert posted[0]["kind"] == "response"
    assert posted[0]["transcript_path"] == str(transcript)


def test_session_start_posts_start_event_with_transcript_path(tmp_path: Path) -> None:
    """The start event is what binds a session to its Matrix thread and its
    transcript reader, so it must reach the daemon intact."""
    result, posted = _run_hook(
        "session_start.sh",
        {"session_id": "s1", "cwd": "/p", "transcript_path": "/p/t.jsonl"},
        tmp_path,
    )

    assert result.returncode == 0
    assert len(posted) == 1
    assert posted[0]["kind"] == "start"
    assert posted[0]["transcript_path"] == "/p/t.jsonl"


def test_hooks_do_not_append_to_shared_absolute_paths() -> None:
    """A debug sink under a world-writable path is both a `set -e` landmine and
    something any local user can pre-create to break the bridge."""
    for script in ("session_start.sh", "prompt.sh", "stop.sh", "notify.sh", "session_end.sh"):
        text = (REPO_ROOT / "hooks" / script).read_text()
        assert ">> /tmp/" not in text, f"{script} appends to a shared /tmp path"


def test_notify_hook_forwards_transcript_path(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    result, posted = _run_hook(
        "notify.sh",
        {
            "session_id": "s1",
            "message": "Claude needs your permission to run Bash",
            "transcript_path": str(transcript),
        },
        tmp_path,
    )

    assert result.returncode == 0
    assert len(posted) == 1
    assert posted[0]["kind"] == "notification"
    assert posted[0]["transcript_path"] == str(transcript)
