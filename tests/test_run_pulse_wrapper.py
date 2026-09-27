"""Tests for bin/run-pulse.py — the launchd pre-flight for the pulse.

In-process tests call the real module with a fake `execv` so the parse check,
the logged skip, and the exact exec command are measured. Subprocess tests copy
the real file next to a stub pulse and let it exec for real.
"""
from __future__ import annotations

import importlib.util
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "bin/run-pulse.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_pulse_mod", str(WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _layout(tmp: Path, pulse_body: str, src_files: dict[str, str] | None = None) -> Path:
    (tmp / "bin").mkdir()
    shutil.copy2(WRAPPER, tmp / "bin/run-pulse.py")
    (tmp / "bin/pulse.py").write_text(pulse_body)
    for rel, body in (src_files or {}).items():
        path = tmp / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp / "bin/run-pulse.py"


def test_healthy_checkout_execs_pulse_with_same_interpreter_and_args():
    mod = _load()
    calls = []
    assert mod.main(["--pulse-idx", "5"], execv=lambda *a: calls.append(a)) == 0
    assert calls == [(sys.executable, [sys.executable, str(REPO / "bin/pulse.py"),
                                       "--pulse-idx", "5"])]


def test_broken_pulse_skips_the_run_and_logs(tmp_path, capsys):
    _layout(tmp_path, "def broken(:\n    pass\n")
    mod = _load()
    calls = []
    with mock.patch.object(mod, "PULSE", tmp_path / "bin/pulse.py"), \
            mock.patch.object(mod, "SRC", tmp_path / "src"):
        assert mod.main([], execv=lambda *a: calls.append(a)) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "pulse pre-flight FAILED, skipping this run" in err
    assert f"{tmp_path / 'bin/pulse.py'}: SyntaxError" in err


def test_broken_src_module_skips_the_run(tmp_path, capsys):
    _layout(tmp_path, "x = 1\n", {"assistant/__init__.py": "",
                                  "assistant/model_tiers.py": "x = 1\n<<<<<<< HEAD\n"})
    mod = _load()
    calls = []
    with mock.patch.object(mod, "PULSE", tmp_path / "bin/pulse.py"), \
            mock.patch.object(mod, "SRC", tmp_path / "src"):
        assert mod.main([], execv=lambda *a: calls.append(a)) == 0
    assert calls == []
    assert "assistant/model_tiers.py: SyntaxError" in capsys.readouterr().err


def test_script_entry_point_execs_pulse():
    calls = []
    with mock.patch.object(os, "execv", lambda *a: calls.append(a)), \
            mock.patch.object(sys, "argv", [str(WRAPPER), "--dry-run"]):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(WRAPPER), run_name="__main__")
    assert exc.value.code == 0
    assert calls == [(sys.executable, [sys.executable, str(REPO / "bin/pulse.py"),
                                       "--dry-run"])]


def test_real_exec_runs_pulse_and_passes_args(tmp_path):
    wrapper = _layout(tmp_path, 'import sys\nprint("RAN", " ".join(sys.argv[1:]))\n',
                      {"assistant/__init__.py": ""})
    r = subprocess.run([sys.executable, str(wrapper), "--pulse-idx", "5"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert r.stdout.strip() == "RAN --pulse-idx 5"
    assert r.stderr == ""


def test_real_run_with_broken_pulse_exits_zero(tmp_path):
    wrapper = _layout(tmp_path, "def broken(:\n")
    r = subprocess.run([sys.executable, str(wrapper)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert r.stdout == ""
    assert "pulse pre-flight FAILED" in r.stderr
