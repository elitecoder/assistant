"""install/patch-settings.py — cmux session hook patcher.

It registers hooks/cmux-auto-resume.py on SessionStart (Claude settings and
Factory hooks) when missing, removes the retired cmux-session-ledger.py
registrations, and never touches hooks other installers own, such as
mukul-memory's cmux-registry.py. Direct patch_path() tests pin what is added,
removed, and kept; CLI tests run main() over several paths, as install.sh does.
On a fresh machine ~/.claude/settings.json does not exist: the file is created
without a backup (an unconditional backup crashed a fresh install on
2026-07-07), and a second run changes nothing.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCH = REPO / "install" / "patch-settings.py"

AUTO_RESUME = "python3 $HOME/.claude/hooks/cmux-auto-resume.py"
LEDGER_START = "python3 $HOME/.claude/hooks/cmux-session-ledger.py start"
LEDGER_END = "python3 $HOME/.claude/hooks/cmux-session-ledger.py end"
REGISTRY = "$HOME/.claude/hooks/cmux-registry.py"
AUTO_RESUME_BLOCK = {"matcher": "", "hooks": [
    {"type": "command", "command": AUTO_RESUME, "timeout": 10}]}


def load_module():
    spec = importlib.util.spec_from_file_location("patch_settings_test", PATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hook(command, timeout=5):
    return {"type": "command", "command": command, "timeout": timeout}


def block(*commands, matcher=""):
    return {"matcher": matcher, "hooks": [hook(c) for c in commands]}


def commands(document, event):
    return [
        entry["command"]
        for item in document.get("hooks", {}).get(event, [])
        for entry in item.get("hooks", [])
    ]


def live_claude_settings():
    """The shape this machine had on 2026-10-19 (install.sh plus mukul-memory)."""
    return {
        "permissions": {"allow": ["Read"]},
        "statusLine": {"command": "echo — status"},
        "hooks": {
            "SessionStart": [
                block(REGISTRY),
                {"matcher": "", "hooks": [hook(AUTO_RESUME, 10)]},
                block(LEDGER_START),
                block("python3 $HOME/.claude/hooks/memory-boot.py"),
            ],
            "SessionEnd": [block(LEDGER_END)],
            "PreToolUse": [block("python3 $HOME/.claude/hooks/block-hook-bypass.py",
                                 matcher="Bash")],
        },
    }


def _run(*paths: Path):
    return subprocess.run([sys.executable, str(PATCH), *map(str, paths)],
                          capture_output=True, text=True, timeout=30)


def test_live_settings_lose_the_ledger_and_keep_everything_else(tmp_path):
    module = load_module()
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(live_claude_settings(), ensure_ascii=False))

    assert module.patch_path(settings) is True

    document = json.loads(settings.read_text())
    assert document["permissions"] == {"allow": ["Read"]}
    assert document["hooks"]["SessionStart"] == [
        block(REGISTRY),
        {"matcher": "", "hooks": [hook(AUTO_RESUME, 10)]},
        block("python3 $HOME/.claude/hooks/memory-boot.py"),
    ]
    assert "SessionEnd" not in document["hooks"]
    assert document["hooks"]["PreToolUse"] == [
        block("python3 $HOME/.claude/hooks/block-hook-bypass.py", matcher="Bash")]
    assert "echo — status" in settings.read_text()  # not escaped to \\u2014
    backups = list(settings.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == live_claude_settings()


def test_auto_resume_is_registered_when_missing(tmp_path):
    module = load_module()
    path = tmp_path / ".factory" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"custom": True, "hooks": {
        "SessionStart": [block(LEDGER_START, "existing-factory", matcher="startup")],
        "Stop": [block("keep-stop")],
    }}))

    assert module.patch_path(path) is True

    assert json.loads(path.read_text()) == {"custom": True, "hooks": {
        "SessionStart": [block("existing-factory", matcher="startup"), AUTO_RESUME_BLOCK],
        "Stop": [block("keep-stop")],
    }}


def test_auto_resume_under_another_path_counts_as_registered(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [
        block("/usr/bin/python3 /custom/hooks/cmux-auto-resume.py")]}}))
    assert module.patch_path(path) is False
    assert not list(tmp_path.glob("settings.json.bak-*"))


def test_only_the_retired_script_is_removed(tmp_path):
    module = load_module()
    kept = [
        REGISTRY,
        "python3 $HOME/bin/not-cmux-session-ledger.py",
        "python3 $HOME/.claude/hooks/cmux-session-ledger.py.disabled",
        "echo cmux-session-ledger",
        "cmux hooks claude session-start",
    ]
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [block(*kept), AUTO_RESUME_BLOCK]}}))

    assert module.patch_path(path) is False
    assert commands(json.loads(path.read_text()), "SessionStart") == kept + [AUTO_RESUME]


def test_leaves_untouched_events_and_blocks_alone(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {
        "SessionStart": [AUTO_RESUME_BLOCK],
        "Stop": [],
        "Notification": [{"matcher": "", "hooks": []}, {"matcher": "x"}],
        "SessionEnd": [block(LEDGER_END)],
    }}))

    assert module.patch_path(path) is True

    assert json.loads(path.read_text()) == {"hooks": {
        "SessionStart": [AUTO_RESUME_BLOCK],
        "Stop": [],
        "Notification": [{"matcher": "", "hooks": []}, {"matcher": "x"}],
    }}


def test_unexpected_shapes_are_skipped_not_fatal(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {
        "Stop": {"not": "a list"},
        "Notification": ["not a block", {"matcher": "", "hooks": "not a list"}],
        "PreToolUse": [{"matcher": "", "hooks": ["not an entry", hook(LEDGER_START)]}],
        "SessionStart": ["not a block", AUTO_RESUME_BLOCK],
    }}))

    assert module.patch_path(path) is True

    assert json.loads(path.read_text()) == {"hooks": {
        "Stop": {"not": "a list"},
        "Notification": ["not a block", {"matcher": "", "hooks": "not a list"}],
        "PreToolUse": [{"matcher": "", "hooks": ["not an entry"]}],
        "SessionStart": ["not a block", AUTO_RESUME_BLOCK],
    }}


def test_a_non_list_session_start_is_left_alone(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": {"odd": True}}}))
    assert module.patch_path(path) is False
    assert json.loads(path.read_text()) == {"hooks": {"SessionStart": {"odd": True}}}


def test_a_non_object_hooks_value_is_left_alone(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text('{"hooks": []}')
    assert module.patch_path(path) is False
    assert path.read_text() == '{"hooks": []}'


def test_fresh_files_are_created_without_a_backup_and_stay_idempotent(tmp_path, capsys):
    module = load_module()
    path = tmp_path / ".factory" / "hooks.json"

    assert module.patch_path(path) is True
    first = path.read_text()
    assert json.loads(first) == {"hooks": {"SessionStart": [AUTO_RESUME_BLOCK]}}
    assert not list(path.parent.glob("hooks.json.bak-*"))

    assert module.patch_path(path) is False
    assert path.read_text() == first
    assert not list(path.parent.glob("hooks.json.bak-*"))
    assert capsys.readouterr().out.rstrip().endswith("no changes needed")


def test_malformed_or_non_object_files_are_backed_up_and_rebuilt(tmp_path):
    module = load_module()
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ not json")
    assert module.patch_path(corrupt) is True
    assert json.loads(corrupt.read_text()) == {"hooks": {"SessionStart": [AUTO_RESUME_BLOCK]}}
    [saved] = tmp_path.glob("corrupt.json.corrupt-*")
    assert saved.read_text() == "{ not json"

    listed = tmp_path / "list.json"
    listed.write_text("[]")
    assert module.patch_path(listed) is True
    assert json.loads(listed.read_text()) == {"hooks": {"SessionStart": [AUTO_RESUME_BLOCK]}}


def test_symlinked_target_is_written_through_not_severed(tmp_path):
    # A machine-config-managed target is a symlink; the atomic write must write
    # THROUGH to the real file and preserve the symlink, not replace it with a
    # regular file (which would disconnect config sync).
    module = load_module()
    real = tmp_path / "machine-config" / "settings.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"existing": "keep", "hooks": {
        "SessionStart": [block(LEDGER_START), block("keep-me")]}}))
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    assert module.patch_path(link) is True

    assert link.is_symlink()
    assert Path(link).resolve() == real.resolve()
    assert json.loads(real.read_text()) == {"existing": "keep", "hooks": {
        "SessionStart": [block("keep-me"), AUTO_RESUME_BLOCK]}}
    assert len(list(real.parent.glob("settings.json.bak-*"))) == 1


def test_cli_patches_claude_and_creates_factory_hooks_in_one_run(tmp_path):
    claude = tmp_path / ".claude" / "settings.json"
    factory = tmp_path / ".factory" / "hooks.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps(live_claude_settings()))

    result = _run(claude, factory)

    assert result.returncode == 0, result.stderr
    assert f"  - SessionStart: {LEDGER_START}" in result.stdout
    assert f"  - SessionEnd: {LEDGER_END}" in result.stdout
    assert "  = SessionStart already runs cmux-auto-resume.py" in result.stdout
    assert f"  + SessionStart: {AUTO_RESUME}" in result.stdout
    assert json.loads(factory.read_text()) == {"hooks": {"SessionStart": [AUTO_RESUME_BLOCK]}}
    remaining = json.loads(claude.read_text())
    assert commands(remaining, "SessionStart") == [
        REGISTRY, AUTO_RESUME, "python3 $HOME/.claude/hooks/memory-boot.py"]
    assert "SessionEnd" not in remaining["hooks"]


def test_cli_patches_the_other_paths_when_one_is_unreadable(tmp_path):
    unreadable = tmp_path / ".claude" / "settings.json"
    unreadable.mkdir(parents=True)
    factory = tmp_path / ".factory" / "hooks.json"
    factory.parent.mkdir()
    factory.write_text(json.dumps({"hooks": {"SessionEnd": [block(LEDGER_END)]}}))

    result = _run(unreadable, factory)

    assert result.returncode == 1
    assert f"{unreadable} could not be patched:" in result.stderr
    assert json.loads(factory.read_text()) == {"hooks": {"SessionStart": [AUTO_RESUME_BLOCK]}}


def test_cli_requires_a_path():
    result = _run()
    assert result.returncode == 2
    assert "usage: patch-settings.py PATH" in result.stderr
