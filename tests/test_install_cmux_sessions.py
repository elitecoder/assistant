"""install.sh section 4: cmux records sessions itself, so the installer removes
the retired hand-written session hooks instead of wiring them, and keeps
hooks/cmux-auto-resume.py, which makes cmux record agents started outside its
Claude wrapper.

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

RETIRED = {
    ".claude/hooks/cmux-session-ledger.py": REPO / "hooks/cmux-session-ledger.py",
    ".local/bin/cmux-restore-sessions": REPO / "bin/cmux-restore-sessions.py",
}
AUTO_RESUME_LINK = ".claude/hooks/cmux-auto-resume.py"
AUTO_RESUME = "python3 $HOME/.claude/hooks/cmux-auto-resume.py"
LEDGER_START = "python3 $HOME/.claude/hooks/cmux-session-ledger.py start"


@pytest.fixture
def install(tmp_path):
    """Run install.sh with these flags against the isolated HOME `tmp_path`."""
    env = installer_env(tmp_path)

    def run(*flags: str) -> subprocess.CompletedProcess:
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


def _seed_previous_install(home: Path) -> dict[str, Path]:
    """Links an older install.sh made, plus links it never owned."""
    links = {}
    for relative, target in RETIRED.items():
        link = home / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        links[relative] = link
    kept = home / AUTO_RESUME_LINK
    kept.symlink_to(REPO / "hooks/cmux-auto-resume.py")
    links["auto-resume"] = kept
    foreign = home / ".claude/hooks/cmux-registry.py"
    foreign.symlink_to(home / "dev/mukul-memory/hooks/cmux-registry.py")
    links["foreign"] = foreign
    elsewhere = home / ".claude/hooks/block-hook-bypass.py"
    elsewhere.symlink_to(REPO / "hooks/block-hook-bypass.py")
    links["elsewhere"] = elsewhere
    settings = home / ".claude/settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": LEDGER_START}]},
        {"matcher": "", "hooks": [{"type": "command", "command": AUTO_RESUME}]},
    ]}}))
    factory = home / ".factory/hooks.json"
    factory.parent.mkdir(parents=True)
    factory.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": LEDGER_START}]},
    ]}}))
    return links


def test_apply_removes_the_retired_links_and_registrations(tmp_path, install):
    links = _seed_previous_install(tmp_path)

    result = install("--apply")

    for relative in RETIRED:
        assert not links[relative].is_symlink(), relative
        assert f"REMOVE retired link {links[relative]}" in result.stdout
    assert os.readlink(links["auto-resume"]) == str(REPO / "hooks/cmux-auto-resume.py")
    assert os.readlink(links["foreign"]) == str(tmp_path / "dev/mukul-memory/hooks/cmux-registry.py")
    assert os.readlink(links["elsewhere"]) == str(REPO / "hooks/block-hook-bypass.py")
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert settings["hooks"] == {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": AUTO_RESUME}]}]}
    assert json.loads((tmp_path / ".factory/hooks.json").read_text())["hooks"] == {}

    again = install("--apply")
    assert "REMOVE retired link" not in again.stdout
    assert not any(links[relative].is_symlink() for relative in RETIRED)


def test_retired_link_into_another_checkout_is_kept(tmp_path, install):
    other = tmp_path / "other-checkout/hooks/cmux-session-ledger.py"
    link = tmp_path / ".claude/hooks/cmux-session-ledger.py"
    link.parent.mkdir(parents=True)
    link.symlink_to(other)

    result = install("--apply")

    assert os.readlink(link) == str(other)
    assert "REMOVE retired link" not in result.stdout


def test_fresh_apply_links_the_auto_resume_hook(tmp_path, install):
    install("--apply")
    assert os.readlink(tmp_path / AUTO_RESUME_LINK) == str(REPO / "hooks/cmux-auto-resume.py")


def test_dry_run_reports_without_removing(tmp_path, install):
    links = _seed_previous_install(tmp_path)
    settings = (tmp_path / ".claude/settings.json").read_text()

    result = install()

    for relative in RETIRED:
        assert links[relative].is_symlink(), relative
        assert f"REMOVE retired link {links[relative]}" in result.stdout
    assert (tmp_path / ".claude/settings.json").read_text() == settings
    assert "would install cmux Factory hooks and patch Claude + Factory lifecycle hooks" in result.stdout


def test_failed_settings_cleanup_warns_and_the_install_continues(tmp_path, install):
    (tmp_path / ".claude/settings.json").mkdir(parents=True)

    result = install("--apply")

    assert ("patch-settings failed (continuing — retired session hooks may still be registered)"
            in result.stderr)
    assert "[5/5] Reloading launchd agents that changed" in result.stdout


def test_help_documents_native_cmux_sessions(tmp_path):
    result = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True,
                            env=installer_env(tmp_path), stdin=subprocess.DEVNULL, timeout=30)
    assert result.returncode == 0
    assert ("  - cmux records agent sessions itself: hooks/cmux-auto-resume.py → ~/.claude/hooks/\n"
            "       for agents started outside cmux's wrapper, Factory hooks via cmux, and the\n"
            "       retired hand-written session hooks removed from Claude and Factory settings\n"
            in result.stdout)
    assert "What is NOT touched:\n  - ~/.claude/cache/, ~/.claude/projects/\n" in result.stdout
    for retired in ("cmux-registry", "cmux-session-ledger", "cmux-restore-sessions"):
        assert retired not in result.stdout
