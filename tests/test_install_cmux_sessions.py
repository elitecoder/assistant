"""install.sh section 4: link the cmux session hooks and restore CLI, register
cmux-auto-resume.py, and drop the retired ledger hook's registrations without
ever leaving a registered command pointing at a missing file.

Runs the real installer against an isolated HOME with recording service
commands (tests/installer_sandbox.py).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from installer_sandbox import assert_recorded_services, installer_env

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"

LINKS = {
    ".claude/hooks/cmux-auto-resume.py": REPO / "hooks/cmux-auto-resume.py",
    ".claude/hooks/cmux-session-ledger.py": REPO / "hooks/cmux-session-ledger.py",
    ".local/bin/cmux-restore-sessions": REPO / "bin/cmux-restore-sessions.py",
}
AUTO_RESUME = "python3 $HOME/.claude/hooks/cmux-auto-resume.py"
LEDGER_START = "python3 $HOME/.claude/hooks/cmux-session-ledger.py start"
LEDGER_END = "python3 $HOME/.claude/hooks/cmux-session-ledger.py end"
REGISTRY = "$HOME/.claude/hooks/cmux-registry.py"


@pytest.fixture
def install(tmp_path):
    """Run install.sh with these flags against the isolated HOME `tmp_path`."""
    env = installer_env(tmp_path)

    def run(*flags: str, ok: bool = True) -> subprocess.CompletedProcess:
        if "--apply" not in flags:
            # A dry run expects a previous install's plists.
            plists = tmp_path / "Library/LaunchAgents"
            plists.mkdir(parents=True, exist_ok=True)
            for template in (REPO / "launchagents").glob("*.plist"):
                (plists / template.name).write_text("<!-- fixture: previous install -->\n")
        result = subprocess.run(["bash", str(INSTALL), *flags], capture_output=True, text=True,
                                env=env, stdin=subprocess.DEVNULL, timeout=120)
        assert result.returncode == 0, f"{result.stdout[-2000:]}\n{result.stderr[-1000:]}"
        assert_recorded_services(tmp_path, apply="--apply" in flags)
        return result

    return run


def hooks(path):
    return {event: [entry["command"] for block in blocks for entry in block["hooks"]]
            for event, blocks in json.loads(path.read_text())["hooks"].items()}


def test_fresh_apply_links_and_registers_the_session_hooks(tmp_path, install):
    result = install("--apply")

    assert "[4/5] Wiring cmux session hooks (hooks + CLI + settings)" in result.stdout
    for relative, target in LINKS.items():
        assert os.readlink(tmp_path / relative) == str(target), relative
    for settings in (".claude/settings.json", ".factory/hooks.json"):
        assert hooks(tmp_path / settings) == {"SessionStart": [AUTO_RESUME]}, settings


def test_apply_on_the_live_layout_drops_only_the_ledger(tmp_path, install):
    (tmp_path / ".claude/hooks").mkdir(parents=True)
    foreign = tmp_path / ".claude/hooks/cmux-registry.py"
    foreign.symlink_to(tmp_path / "dev/mukul-memory/hooks/cmux-registry.py")
    settings = tmp_path / ".claude/settings.json"
    settings.write_text(json.dumps({"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": c}]}
                         for c in (REGISTRY, AUTO_RESUME, LEDGER_START)],
        "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": LEDGER_END}]}],
    }}))

    install("--apply")

    assert hooks(settings) == {"SessionStart": [REGISTRY, AUTO_RESUME]}
    assert os.readlink(foreign) == str(tmp_path / "dev/mukul-memory/hooks/cmux-registry.py")
    patched = settings.read_text()
    backups = sorted(settings.parent.glob("settings.json.bak-*"))

    again = install("--apply")
    assert settings.read_text() == patched
    assert sorted(settings.parent.glob("settings.json.bak-*")) == backups
    assert "no changes needed" in again.stdout


def test_a_ledger_registration_left_by_the_pull_still_exits_cleanly(tmp_path, install):
    # Between `git pull` and `install.sh --apply` the old registration still
    # runs through the old link; the file it reaches must not be missing.
    install("--apply")
    result = subprocess.run(
        ["bash", "-c", LEDGER_START], input='{"session_id": "sid"}', capture_output=True,
        text=True, timeout=30, env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / ".cmux-session-ledger.jsonl").exists()


def test_dry_run_reports_without_patching(tmp_path, install):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionEnd": [
        {"matcher": "", "hooks": [{"type": "command", "command": LEDGER_END}]}]}}))

    result = install()

    assert "would install cmux Factory hooks and patch Claude + Factory session hooks" in result.stdout
    assert hooks(settings) == {"SessionEnd": [LEDGER_END]}


def test_failed_settings_patch_warns_and_the_install_continues(tmp_path, install):
    (tmp_path / ".claude/settings.json").mkdir(parents=True)

    result = install("--apply")

    assert ("patch-settings failed (continuing — cmux session hooks may be incomplete)"
            in result.stderr)
    assert hooks(tmp_path / ".factory/hooks.json") == {"SessionStart": [AUTO_RESUME]}
    assert "[5/5] Reloading launchd agents that changed" in result.stdout


def test_help_documents_the_cmux_session_hooks(tmp_path):
    result = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True,
                            env=installer_env(tmp_path), stdin=subprocess.DEVNULL, timeout=30)
    assert result.returncode == 0
    assert ("  - cmux session hooks: hooks/ → ~/.claude/hooks/ (symlinks),\n"
            "       bin/cmux-restore-sessions.py → ~/.local/bin/cmux-restore-sessions,\n"
            "       and Claude + Factory SessionStart patched to run cmux-auto-resume.py\n"
            "       (the retired ledger hook's registrations removed)\n" in result.stdout)
