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
