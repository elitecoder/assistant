"""install.sh links each file in commands/ into ~/.claude/commands/ by name,
backs up a file already there, and leaves other commands alone.

Runs the real installer against an isolated HOME with recording service
commands (tests/installer_sandbox.py).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from installer_sandbox import assert_recorded_services, installer_env

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"
COMMANDS = ("close-workspace.md", "close-workspace.py")


def install(home: Path, *flags: str) -> subprocess.CompletedProcess:
    env = installer_env(home)
    if "--apply" not in flags:
        plists = home / "Library/LaunchAgents"
        plists.mkdir(parents=True, exist_ok=True)
        for template in (REPO / "launchagents").glob("*.plist"):
            (plists / template.name).write_text("<!-- fixture: previous install -->\n")
    result = subprocess.run(["bash", str(INSTALL), *flags], capture_output=True, text=True,
                            env=env, stdin=subprocess.DEVNULL, timeout=120)
    assert result.returncode == 0, f"{result.stdout[-2000:]}\n{result.stderr[-1000:]}"
    assert_recorded_services(home, apply="--apply" in flags)
    return result


def test_apply_links_every_repo_command_and_keeps_the_old_file(tmp_path):
    commands = tmp_path / ".claude/commands"
    commands.mkdir(parents=True)
    (commands / "close-workspace.md").write_text("old close-workspace\n")
    (commands / "title.md").write_text("someone else's command\n")

    install(tmp_path, "--apply")

    linked = {p.name for p in commands.iterdir() if p.is_symlink()}
    assert linked == set(COMMANDS)
    for name in COMMANDS:
        assert (commands / name).resolve() == (REPO / "commands" / name).resolve(), name
    backups = list(commands.glob("close-workspace.md.bak-*"))
    assert [b.read_text() for b in backups] == ["old close-workspace\n"]
    assert (commands / "title.md").read_text() == "someone else's command\n"
    assert not (commands / "title.md").is_symlink()


def test_dry_run_links_no_commands(tmp_path):
    result = install(tmp_path)
    assert "NEW  " + str(tmp_path / ".claude/commands/close-workspace.md") in result.stdout
    assert not (tmp_path / ".claude/commands/close-workspace.md").exists()


def test_help_documents_the_commands_link(tmp_path):
    result = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True,
                            env=installer_env(tmp_path), stdin=subprocess.DEVNULL, timeout=30)
    assert result.returncode == 0
    assert ("  - ~/.claude/commands/<file>                    → symlink → "
            f"{REPO}/commands/<file>\n"
            "    (/close-workspace and its script; other commands are left alone)\n" in result.stdout)
