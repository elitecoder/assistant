"""install/patch-settings.py removes the retired cmux session hooks.

cmux records agent sessions itself, so the installer unregisters the
hand-written cmux-session-ledger.py and cmux-registry.py hooks from Claude
settings and Factory hooks, and never adds them back. cmux-auto-resume.py stays
registered: it is what makes cmux record agents started without its wrapper.
Direct patch_path() tests pin what is removed and what survives; CLI tests run
main() over several paths, as install.sh does.
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
    """The shape install.sh and mukul-memory's link-hooks left on a real machine."""
    return {
        "permissions": {"allow": ["Read"]},
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


def test_removes_every_retired_hook_and_keeps_the_rest(tmp_path):
    module = load_module()
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(live_claude_settings()))

    assert module.patch_path(settings) is True

    document = json.loads(settings.read_text())
    assert document["permissions"] == {"allow": ["Read"]}
    assert document["hooks"]["SessionStart"] == [
        {"matcher": "", "hooks": [hook(AUTO_RESUME, 10)]},
        block("python3 $HOME/.claude/hooks/memory-boot.py"),
    ]
    assert "SessionEnd" not in document["hooks"]
    assert document["hooks"]["PreToolUse"] == [
        block("python3 $HOME/.claude/hooks/block-hook-bypass.py", matcher="Bash")]
    backups = list(settings.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == live_claude_settings()


def test_mixed_block_keeps_its_matcher_and_other_hooks(tmp_path):
    module = load_module()
    path = tmp_path / ".factory" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"custom": True, "hooks": {
        "SessionStart": [block(REGISTRY, "existing-factory", LEDGER_START, matcher="startup")],
    }}))

    assert module.patch_path(path) is True

    assert json.loads(path.read_text()) == {"custom": True, "hooks": {
        "SessionStart": [block("existing-factory", matcher="startup")],
    }}


def test_leaves_untouched_events_and_blocks_alone(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {
        "Stop": [],
        "Notification": [{"matcher": "", "hooks": []}, {"matcher": "x"}],
        "SessionEnd": [block(LEDGER_END)],
    }}))

    assert module.patch_path(path) is True

    assert json.loads(path.read_text()) == {"hooks": {
        "Stop": [],
        "Notification": [{"matcher": "", "hooks": []}, {"matcher": "x"}],
    }}


def test_only_the_retired_scripts_match(tmp_path):
    module = load_module()
    kept = [
        AUTO_RESUME,
        "python3 $HOME/bin/not-cmux-registry.py",
        "python3 $HOME/.claude/hooks/cmux-registry.py.disabled",
        "echo cmux-session-ledger",
        "cmux hooks claude session-start",
    ]
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [block(*kept)]}}))

    assert module.patch_path(path) is False
    assert commands(json.loads(path.read_text()), "SessionStart") == kept
    assert not list(tmp_path.glob("settings.json.bak-*"))


def test_unexpected_shapes_are_skipped_not_fatal(tmp_path):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {
        "Stop": {"not": "a list"},
        "Notification": ["not a block", {"matcher": "", "hooks": "not a list"}],
        "PreToolUse": [{"matcher": "", "hooks": ["not an entry", hook(REGISTRY)]}],
        "SessionEnd": [block(LEDGER_END)],
    }}))

    assert module.patch_path(path) is True

    assert json.loads(path.read_text()) == {"hooks": {
        "Stop": {"not": "a list"},
        "Notification": ["not a block", {"matcher": "", "hooks": "not a list"}],
        "PreToolUse": [{"matcher": "", "hooks": ["not an entry"]}],
    }}


def test_second_run_changes_nothing(tmp_path, capsys):
    module = load_module()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(live_claude_settings()))
    assert module.patch_path(path) is True
    first = path.read_text()

    assert module.patch_path(path) is False

    assert path.read_text() == first
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 1
    assert capsys.readouterr().out.rstrip().endswith("no changes needed")


def test_missing_file_is_not_created(tmp_path):
    module = load_module()
    path = tmp_path / ".factory" / "hooks.json"
    assert module.patch_path(path) is False
    assert not path.parent.exists()


def test_malformed_or_unexpected_documents_are_left_untouched(tmp_path):
    module = load_module()
    for name, text in [("corrupt.json", "{ not json"), ("list.json", "[]"),
                       ("no-hooks.json", '{"model": "opus"}'),
                       ("bad-hooks.json", '{"hooks": []}')]:
        path = tmp_path / name
        path.write_text(text)
        assert module.patch_path(path) is False
        assert path.read_text() == text
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "bad-hooks.json", "corrupt.json", "list.json", "no-hooks.json"]


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
        "SessionStart": [block("keep-me")]}}
    assert len(list(real.parent.glob("settings.json.bak-*"))) == 1


def test_cli_cleans_claude_and_factory_files_in_one_run(tmp_path):
    claude = tmp_path / ".claude" / "settings.json"
    factory = tmp_path / ".factory" / "hooks.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps(live_claude_settings()))

    result = _run(claude, factory)

    assert result.returncode == 0, result.stderr
    assert f"  - SessionStart: {REGISTRY}" in result.stdout
    assert f"  - SessionEnd: {LEDGER_END}" in result.stdout
    assert f"{factory} does not exist" in result.stdout
    assert not factory.exists()
    remaining = json.loads(claude.read_text())
    assert commands(remaining, "SessionStart") == [
        AUTO_RESUME, "python3 $HOME/.claude/hooks/memory-boot.py"]
    assert "SessionEnd" not in remaining["hooks"]


def test_cli_cleans_the_other_paths_when_one_is_unreadable(tmp_path):
    unreadable = tmp_path / ".claude" / "settings.json"
    unreadable.mkdir(parents=True)
    factory = tmp_path / ".factory" / "hooks.json"
    factory.parent.mkdir()
    factory.write_text(json.dumps({"hooks": {"SessionEnd": [block(LEDGER_END)]}}))

    result = _run(unreadable, factory)

    assert result.returncode == 1
    assert f"{unreadable} could not be cleaned:" in result.stderr
    assert json.loads(factory.read_text()) == {"hooks": {}}


def test_cli_requires_a_path():
    result = _run()
    assert result.returncode == 2
    assert "usage: patch-settings.py PATH" in result.stderr
