"""patch-settings.py — hook patcher coverage.

Two layers:
  - Direct patch_path() unit tests: preserves existing hooks, no backup for a
    newly-created file, and (the load-bearing one) writes THROUGH a symlinked
    target instead of severing it (machine-config sync must survive an --apply).
  - Fresh-machine CLI tests (subprocess, exercising main()'s multi-path loop):
    on a truly fresh machine ~/.claude/settings.json does not exist, so an
    unconditional `shutil.copy2(path, bak)` backup crashed with FileNotFoundError
    (caught by a live fresh-install run 2026-07-07). The installer must create
    the file fresh instead of backing up a nonexistent one, stay idempotent on a
    second run, and preserve unrelated user settings.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCH = REPO / "install" / "patch-settings.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "patch_settings_test", REPO / "install" / "patch-settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commands(document, event):
    return [
        hook["command"]
        for block in document.get("hooks", {}).get(event, [])
        for hook in block.get("hooks", [])
    ]


def _run(path: Path):
    return subprocess.run([sys.executable, str(PATCH), str(path)],
                          capture_output=True, text=True, timeout=30)


def _session_start_cmds(data):
    out = []
    for block in data["hooks"].get("SessionStart", []):
        for h in block.get("hooks", []):
            out.append(h.get("command", ""))
    return out


# --------------------------------------------------------------------------- direct patch_path()

def test_patches_claude_and_factory_without_deleting_existing_hooks(tmp_path):
    module = load_module()
    claude = tmp_path / ".claude" / "settings.json"
    factory = tmp_path / ".factory" / "hooks.json"
    claude.parent.mkdir()
    factory.parent.mkdir()
    claude.write_text(json.dumps({
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": "existing-claude"}],
            }],
            "Stop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": module.LEDGER_END_CMD,
                }],
            }],
        },
    }))
    factory.write_text(json.dumps({
        "custom": True,
        "hooks": {
            "PostToolUse": [{
                "matcher": "Edit",
                "hooks": [{"type": "command", "command": "existing-factory"}],
            }],
        },
    }))

    assert module.patch_path(claude)
    assert module.patch_path(factory)

    claude_doc = json.loads(claude.read_text())
    factory_doc = json.loads(factory.read_text())
    assert claude_doc["permissions"] == {"allow": ["Read"]}
    assert "existing-claude" in commands(claude_doc, "SessionStart")
    assert module.LEDGER_END_CMD not in commands(claude_doc, "Stop")
    assert factory_doc["custom"] is True
    assert "existing-factory" in commands(factory_doc, "PostToolUse")
    for document in (claude_doc, factory_doc):
        assert module.AUTO_RESUME_CMD in commands(document, "SessionStart")
        assert module.LEDGER_START_CMD in commands(document, "SessionStart")
        assert module.LEDGER_END_CMD in commands(document, "SessionEnd")

    assert len(list(claude.parent.glob("settings.json.bak-*"))) == 1
    assert len(list(factory.parent.glob("hooks.json.bak-*"))) == 1


def test_new_factory_hooks_file_has_no_backup_and_is_idempotent(tmp_path):
    module = load_module()
    path = tmp_path / ".factory" / "hooks.json"

    assert module.patch_path(path)
    first = path.read_text()
    assert not list(path.parent.glob("hooks.json.bak-*"))

    assert module.patch_path(path) is False
    assert path.read_text() == first
    assert not list(path.parent.glob("hooks.json.bak-*"))


def test_symlinked_target_is_written_through_not_severed(tmp_path):
    # A machine-config-managed target is a symlink; the atomic write must write
    # THROUGH to the real file and preserve the symlink, not replace it with a
    # regular file (which would disconnect config sync).
    module = load_module()
    real = tmp_path / "machine-config" / "settings.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"existing": "keep"}))
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    assert module.patch_path(link)
    # The symlink is intact and still points at the real file.
    assert link.is_symlink()
    assert Path(link).resolve() == real.resolve()
    # The hooks landed in the REAL file (written through), preserving prior keys.
    doc = json.loads(real.read_text())
    assert doc["existing"] == "keep"
    assert "SessionStart" in doc["hooks"]


# --------------------------------------------------------------------------- fresh-machine CLI (main())

def test_fresh_machine_no_settings_file(tmp_path):
    # settings.json absent (+ a not-yet-created .claude dir) — the fresh case.
    settings = tmp_path / ".claude" / "settings.json"
    r = _run(settings)
    assert r.returncode == 0, f"patch-settings crashed on a fresh machine:\n{r.stderr}"
    assert settings.exists(), "settings.json should be created fresh"
    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    assert "SessionStart" in hooks and "SessionEnd" in hooks
    # commands live at SessionStart[].hooks[].command (nested matcher blocks)
    cmds = _session_start_cmds(data)
    assert any("cmux-auto-resume.py" in c for c in cmds)
    assert any("cmux-session-ledger.py start" in c for c in cmds)


def test_no_backup_created_when_file_absent(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    _run(settings)
    baks = list((tmp_path / ".claude").glob("settings.json.bak-*"))
    assert not baks, f"no .bak should be made for a file that never existed: {baks}"


def test_idempotent_second_run(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    _run(settings)                      # fresh create
    first = json.loads(settings.read_text())
    r2 = _run(settings)                 # re-run on existing file
    assert r2.returncode == 0
    second = json.loads(settings.read_text())
    # no duplicate hook commands added on the second pass
    assert _session_start_cmds(first) == _session_start_cmds(second), \
        "second run must not duplicate hooks"


def test_preserves_existing_user_settings(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus", "hooks": {}}))
    r = _run(settings)
    assert r.returncode == 0
    data = json.loads(settings.read_text())
    assert data["model"] == "opus", "must preserve unrelated user settings"
    # and a backup of the pre-existing file WAS made
    assert list((tmp_path / ".claude").glob("settings.json.bak-*")), "existing file should be backed up"
