import json
import subprocess
from pathlib import Path


def _run_install(
    repo_root: Path,
    home: Path,
    script: str = "install.sh",
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo_root / "hooks" / script)],
        env={"HOME": str(home), "PATH": "/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
    )


def test_install_creates_settings_with_hooks(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    claude_home = tmp_path
    (claude_home / ".claude").mkdir()
    result = _run_install(repo_root, claude_home)
    assert result.returncode == 0, result.stderr
    settings = json.loads((claude_home / ".claude" / "settings.json").read_text())
    assert "hooks" in settings
    hook_blocks = settings["hooks"]
    for key in ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd"):
        assert key in hook_blocks
        command = hook_blocks[key][-1]["hooks"][0]["command"]
        assert command.startswith("SLACKBOT_AGENT=claude ")
    installed_dir = claude_home / ".claude" / "hooks" / "claude-slack-bot"
    assert (installed_dir / "session_start.sh").is_file()
    assert (installed_dir / "session_start.sh").stat().st_mode & 0o111


def test_install_is_idempotent(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    claude_home = tmp_path
    (claude_home / ".claude").mkdir()
    _run_install(repo_root, claude_home)
    first = (claude_home / ".claude" / "settings.json").read_text()
    _run_install(repo_root, claude_home)
    second = (claude_home / ".claude" / "settings.json").read_text()
    assert first == second


def test_install_codex_preserves_existing_hooks(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    codex_home = tmp_path
    (codex_home / ".codex").mkdir()
    (codex_home / ".codex" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo existing",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    result = _run_install(repo_root, codex_home, "install-codex.sh")
    assert result.returncode == 0, result.stderr

    settings = json.loads((codex_home / ".codex" / "hooks.json").read_text())
    stop_commands = [
        hook["command"] for block in settings["hooks"]["Stop"] for hook in block["hooks"]
    ]
    assert "echo existing" in stop_commands
    assert any(command.startswith("SLACKBOT_AGENT=codex ") for command in stop_commands)
    assert "UserPromptSubmit" in settings["hooks"]


def test_install_gemini_uses_gemini_events(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    gemini_home = tmp_path
    (gemini_home / ".gemini").mkdir()
    result = _run_install(repo_root, gemini_home, "install-gemini.sh")
    assert result.returncode == 0, result.stderr

    settings = json.loads((gemini_home / ".gemini" / "settings.json").read_text())
    for key in ("SessionStart", "BeforeAgent", "AfterAgent", "Notification", "SessionEnd"):
        assert key in settings["hooks"]
    after_agent_command = settings["hooks"]["AfterAgent"][-1]["hooks"][0]["command"]
    assert after_agent_command.startswith("SLACKBOT_AGENT=gemini ")
