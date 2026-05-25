#!/usr/bin/env python3
"""Install Slack bridge hooks into one agent's hook configuration."""

from __future__ import annotations

import argparse
import json
import shutil
import stat
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--hooks-dir", required=True)
    parser.add_argument("events", nargs="+")
    args = parser.parse_args()

    source_dir = Path(__file__).resolve().parent
    hooks_dir = Path(args.hooks_dir).expanduser()
    settings_path = Path(args.settings).expanduser()
    event_specs = [_parse_event(event) for event in args.events]

    hooks_dir.mkdir(parents=True, exist_ok=True)
    for _event, script_name in event_specs:
        destination = hooks_dir / script_name
        shutil.copy2(source_dir / script_name, destination)
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)

    settings = _read_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"{settings_path} has non-object hooks field")

    for event_name, script_name in event_specs:
        command = f"SLACKBOT_AGENT={args.agent} {hooks_dir / script_name}"
        blocks = hooks.setdefault(event_name, [])
        if not isinstance(blocks, list):
            raise RuntimeError(f"{settings_path} hooks.{event_name} is not an array")
        marker = f"/claude-slack-bot/{script_name}"
        hooks[event_name] = [
            block for block in blocks if not _block_has_command(block, command, marker)
        ]
        hooks[event_name].append({"hooks": [{"type": "command", "command": command}]})

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    print(f"Installed {args.agent} hooks into {hooks_dir}")
    print(f"Updated {settings_path}")


def _parse_event(value: str) -> tuple[str, str]:
    try:
        event_name, script_name = value.split(":", 1)
    except ValueError as exc:
        raise RuntimeError(f"event spec must be EVENT:script.sh, got {value!r}") from exc
    return event_name, script_name


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _block_has_command(block: Any, command: str, marker: str) -> bool:
    if not isinstance(block, dict):
        return False
    hook_list = block.get("hooks", [])
    if not isinstance(hook_list, list):
        return False
    for hook in hook_list:
        if not isinstance(hook, dict):
            continue
        existing = hook.get("command")
        if existing == command or (isinstance(existing, str) and marker in existing):
            return True
    return False


if __name__ == "__main__":
    main()
