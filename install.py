"""Install the local MCP server and Codex hooks, preserving other settings."""
import argparse
import json
import os
from pathlib import Path
import shlex
import time
import tomllib


def write_with_backup(path, old, new):
    if old == new:
        return
    current = path.read_text() if path.exists() else ""
    if current != old:
        raise RuntimeError(f"{path} changed during installation; retry.")
    if path.exists():
        backup = path.with_name(path.name + f".agent-talks-backup-{time.time_ns()}")
        backup.write_text(old)
        backup.chmod(0o600)
    temp = path.with_name(path.name + f".agent-talks-{os.getpid()}.tmp")
    temp.write_text(new)
    temp.chmod(0o600)
    temp.replace(path)


def install(home):
    root = Path(__file__).resolve().parent
    python = root / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Create .venv and install requirements.txt first.")
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    old = config.read_text() if config.exists() else ""
    parsed = tomllib.loads(old)
    desired = {"command": str(python), "args": [str(root / "server.py")], "tool_timeout_sec": 3700}
    existing = parsed.get("mcp_servers", {}).get("agent_talks")
    if existing is not None and existing != desired:
        raise RuntimeError("An agent_talks MCP entry already exists with different settings. Review it before installing.")
    new = old
    if existing is None:
        new = old.rstrip() + "\n\n[mcp_servers.agent_talks]\n"
        new += f"command = {json.dumps(str(python))}\nargs = [{json.dumps(str(root / 'server.py'))}]\ntool_timeout_sec = 3700\n"
    hooks_path = home / "hooks.json"
    old_hooks = hooks_path.read_text() if hooks_path.exists() else ""
    hooks = json.loads(old_hooks or "{}")
    command = shlex.join([str(python), str(root / "hook.py")])
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        groups = hooks.setdefault("hooks", {}).setdefault(event, [])
        if not any(h.get("command") == command for group in groups for h in group.get("hooks", [])):
            group = {"hooks": [{"type": "command", "command": command, "timeout": 15}]}
            if event == "PostToolUse":
                group["matcher"] = "^mcp__agent_talks__send$"
            groups.append(group)
    write_with_backup(config, old, new)
    serialized = json.dumps(hooks, indent=2) + "\n"
    if json.loads(old_hooks or "{}") != hooks:
        write_with_backup(hooks_path, old_hooks, serialized)
    print('Provided as is, without warranty. You accept all risk from installation and use.')
    print(f"Installed agent_talks in {config}")
    print(f"Installed hooks in {hooks_path}")
    print("In Codex, use /hooks to review and trust the three new Agent Talks hooks.")
    print("Restart or resume both chats to load the MCP server. Keep both chats open while paired.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, epilog='Provided as is, without warranty. You accept all risk from installation and use.')
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    install(parser.parse_args().home)
