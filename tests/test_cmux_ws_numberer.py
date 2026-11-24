"""cmux-ws-numberer startup — log dir creation and log rotation."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "cmux-ws-numberer.py"


def test_import_creates_missing_log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("cmux_ws_numberer_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.LOG_PATH == tmp_path / ".assistant" / "logs" / "cmux-ws-numberer.log"
    assert mod.LOG_PATH.parent.is_dir()


def test_log_rotates_in_a_fresh_process(tmp_path):
    probe = (
        "import importlib.util, logging, sys\n"
        "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "h = logging.getLogger().handlers[0]\n"
        "print(type(h).__name__, h.baseFilename, h.maxBytes, h.backupCount)\n"
    )
    env = {**os.environ, "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-I", "-c", probe, str(SCRIPT)],
                       capture_output=True, text=True, env=env, timeout=30, check=True)
    log_path = tmp_path / ".assistant" / "logs" / "cmux-ws-numberer.log"
    assert r.stdout.split() == ["RotatingFileHandler", str(log_path), "5000000", "2"]


# --- a refused connection is logged once, then summarised ----------------------

def _load_numberer(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("cmux_ws_numberer_dedupe", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Capture:
    def __init__(self):
        self.lines = []

    def error(self, msg, *args):
        self.lines.append(msg % args)

    info = error


def _cmux(mod, monkeypatch, stderr, stdout=""):
    def run(*a, **k):
        return subprocess.CompletedProcess(a, 1 if stderr else 0, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(mod.subprocess, "run", run)


def _failing_cmux(mod, monkeypatch, stderr):
    _cmux(mod, monkeypatch, stderr)


def test_a_repeated_refusal_is_logged_once_then_once_per_interval(tmp_path, monkeypatch):
    mod = _load_numberer(tmp_path, monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(mod, "log", cap)
    _failing_cmux(mod, monkeypatch, "Error: No live cmux socket found")
    clock = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    for _ in range(201):                       # ten minutes of 3-second polls
        assert mod.list_workspaces() == []
        clock[0] += 3
    assert len(cap.lines) == 3                 # the first, then one per 5 minutes
    assert "No live cmux socket" in cap.lines[0]
    assert "still failing (101 failures since " in cap.lines[1]


def test_a_different_error_is_logged_at_once(tmp_path, monkeypatch):
    mod = _load_numberer(tmp_path, monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(mod, "log", cap)
    _failing_cmux(mod, monkeypatch, "Error: No live cmux socket found")
    mod.list_workspaces()
    _failing_cmux(mod, monkeypatch, "Access denied - only processes started inside cmux can connect")
    mod.list_workspaces()
    assert len(cap.lines) == 2
    assert "Access denied" in cap.lines[1]


def test_an_error_that_differs_only_in_digits_is_the_same_error(tmp_path, monkeypatch):
    mod = _load_numberer(tmp_path, monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(mod, "log", cap)
    for pid in (4101, 4102, 4103):
        _failing_cmux(mod, monkeypatch, f"Error: socket for pid {pid} is gone")
        mod.list_workspaces()
    assert len(cap.lines) == 1
    assert "pid 4101" in cap.lines[0]


def test_a_working_call_clears_the_repeat_state_and_says_it_recovered(tmp_path, monkeypatch):
    mod = _load_numberer(tmp_path, monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(mod, "log", cap)
    _failing_cmux(mod, monkeypatch, "Error: No live cmux socket found")
    for _ in range(3):
        mod.list_workspaces()
    _cmux(mod, monkeypatch, "", stdout='{"workspaces": []}')
    assert mod.list_workspaces() == []
    assert cap.lines[-1].startswith("list-workspaces recovered after 3 failures (since ")
    assert mod._repeat["key"] is None
    mod.list_workspaces()                      # still fine: no second recovery line
    assert len(cap.lines) == 2
    _failing_cmux(mod, monkeypatch, "Error: No live cmux socket found")
    mod.list_workspaces()                      # the same error after a recovery is new again
    assert len(cap.lines) == 3 and "No live cmux socket" in cap.lines[2]
