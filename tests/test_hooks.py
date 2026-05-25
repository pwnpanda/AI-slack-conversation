import json
import subprocess
from pathlib import Path


def _run_prompt_hook(agent: str, prompt: str, tmp_path: Path) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parent.parent
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n")
    curl.chmod(0o755)
    return subprocess.run(
        ["bash", str(repo_root / "hooks" / "prompt.sh")],
        input=json.dumps({"session_id": "s1", "prompt": prompt}),
        env={
            "PATH": f"{bin_dir}:/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin",
            "SLACKBOT_AGENT": agent,
            "SLACKBOT_PORT": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_codex_rename_prompt_blocks_model_submission(tmp_path: Path) -> None:
    result = _run_prompt_hook("codex", "rn named-session", tmp_path)

    assert result.returncode == 2
    assert result.stdout == "{}\n"
    assert "Renamed Slack thread to named-session" in result.stderr


def test_claude_rename_prompt_does_not_block(tmp_path: Path) -> None:
    result = _run_prompt_hook("claude", "rn named-session", tmp_path)

    assert result.returncode == 0
    assert result.stdout == "{}\n"
