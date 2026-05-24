import json
import subprocess
from pathlib import Path


def _run_install(repo_root: Path, claude_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo_root / "hooks" / "install.sh")],
        env={"HOME": str(claude_home), "PATH": "/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin"},
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
