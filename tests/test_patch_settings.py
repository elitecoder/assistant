"""patch-settings.py — fresh-machine gate.

Regression: on a truly fresh machine ~/.claude/settings.json does not exist, so
the installer's `shutil.copy2(path, bak)` backup crashed with FileNotFoundError
(caught by a live fresh-install run 2026-07-07). The installer must create the
file fresh instead of backing up a nonexistent one. Also verify idempotence: a
second run on the now-existing file backs it up and adds no duplicate hooks.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCH = REPO / "install" / "patch-settings.py"


def _run(path: Path):
    return subprocess.run([sys.executable, str(PATCH), str(path)],
                          capture_output=True, text=True, timeout=30)


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


def _session_start_cmds(data):
    out = []
    for block in data["hooks"].get("SessionStart", []):
        for h in block.get("hooks", []):
            out.append(h.get("command", ""))
    return out


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
