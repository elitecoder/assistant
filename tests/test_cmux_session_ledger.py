"""hooks/cmux-session-ledger.py is a retired no-op kept for one release: a
SessionStart/SessionEnd registration that outlives the pull must exit 0
without writing the old ledger."""
from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "cmux-session-ledger.py"
# Lets a coverage run measure the stub's process.
COVERAGE_ENV = {k: v for k, v in os.environ.items() if k.startswith("COVERAGE")}


@pytest.mark.parametrize("event", ["start", "end"])
def test_registered_stub_drains_stdin_and_exits_cleanly(tmp_path, event):
    payload = json.dumps({"session_id": "sid", "cwd": "/r"}) * 2000
    result = subprocess.run([sys.executable, str(HOOK), event], input=payload,
                            capture_output=True, text=True, timeout=30,
                            env={"HOME": str(tmp_path), "CMUX_SURFACE_ID": "surface-1",
                                 **COVERAGE_ENV})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / ".cmux-session-ledger.jsonl").exists()
    assert [p for p in tmp_path.iterdir() if not p.name.startswith(".coverage")] == []


def test_stub_run_by_hand_does_not_wait_for_input(tmp_path):
    controller, terminal = pty.openpty()
    try:
        result = subprocess.run([sys.executable, str(HOOK), "start"], stdin=terminal,
                                capture_output=True, timeout=10,
                                env={"HOME": str(tmp_path), **COVERAGE_ENV})
    finally:
        os.close(terminal)
        os.close(controller)
    assert result.returncode == 0
