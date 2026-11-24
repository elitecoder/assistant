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


def _failing_cmux(mod, monkeypatch, stderr):
    def run(*a, **k):
        return subprocess.CompletedProcess(a, 1, stdout="", stderr=stderr)
    monkeypatch.setattr(mod.subprocess, "run", run)


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
    assert "still failing" in cap.lines[1]
    assert "100 more times" in cap.lines[1]


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
