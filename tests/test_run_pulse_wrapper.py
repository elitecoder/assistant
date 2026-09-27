"""Integration tests for bin/run-pulse.sh — the launchd pre-flight wrapper.

Drives the REAL shell script (byte-for-byte copied into a throwaway repo layout
with a stub pulse.py) so the compile-check / exit-0-on-failure / arg-passthrough
behavior is exercised end to end, not asserted from reading the source. This is
the guard that keeps a bad pulse.py from crash-looping launchd into a silent,
throttled outage.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "bin/run-pulse.sh"


def _make_layout(tmp: Path, pulse_body: str) -> tuple[Path, Path]:
    """Copy the real wrapper into tmp/bin next to a stub pulse.py. Returns
    (wrapper_path, fake_home)."""
    (tmp / "bin").mkdir()
    dst = tmp / "bin/run-pulse.sh"
    shutil.copy2(WRAPPER, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (tmp / "bin/pulse.py").write_text(pulse_body)
    home = tmp / "home"
    home.mkdir()
    return dst, home


def _run(wrapper: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([str(wrapper), *args], capture_output=True, text=True,
                          env=env, timeout=60)


def test_committed_wrapper_is_executable_with_shebang():
    assert WRAPPER.exists()
    assert os.access(WRAPPER, os.X_OK), "run-pulse.sh must be executable for launchd"
    assert WRAPPER.read_text().startswith("#!/bin/bash")


def test_good_pulse_runs_with_no_extra_args(tmp_path):
    # The real plist invocation is `run-pulse.sh <python>` with nothing after
    # it, so exec "$@" runs with an empty $@ under `set -u`. Pin that path.
    wrapper, home = _make_layout(tmp_path, 'print("RAN")\n')
    r = _run(wrapper, home, "python3")
    assert r.returncode == 0
    assert "RAN" in r.stdout
    assert not (home / ".assistant/logs/assistant-pulse.launchd.err").exists()


def test_good_pulse_runs_and_passes_args(tmp_path):
    wrapper, home = _make_layout(
        tmp_path, 'import sys\nprint("RAN", " ".join(sys.argv[1:]))\n')
    r = _run(wrapper, home, "python3", "--pulse-idx", "5")
    assert r.returncode == 0
    assert "RAN --pulse-idx 5" in r.stdout
    # A healthy run leaves no failure log behind.
    assert not (home / ".assistant/logs/assistant-pulse.launchd.err").exists()


def test_broken_pulse_exits_zero_and_logs(tmp_path):
    wrapper, home = _make_layout(tmp_path, "def broken(:\n    pass\n")
    r = _run(wrapper, home, "python3")
    # Exit 0 is the whole point: a non-zero exit would make launchd throttle.
    assert r.returncode == 0, f"wrapper must exit 0 on compile failure, got {r.returncode}"
    err_log = home / ".assistant/logs/assistant-pulse.launchd.err"
    assert err_log.exists(), "compile failure must be logged for the operator"
    text = err_log.read_text()
    assert "pre-flight py_compile FAILED" in text
    assert "SyntaxError" in text


def test_conflict_markers_block_the_run(tmp_path):
    wrapper, home = _make_layout(
        tmp_path, "x = 1\n<<<<<<< HEAD\ny = 2\n=======\ny = 3\n>>>>>>> other\n")
    r = _run(wrapper, home, "python3")
    assert r.returncode == 0
    assert (home / ".assistant/logs/assistant-pulse.launchd.err").exists()
