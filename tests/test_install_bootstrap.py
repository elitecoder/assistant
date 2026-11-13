"""install-bootstrap.sh's prerequisite check: Claude, Copilot, or Droid is
enough to continue. Runs the real script with a stand-in git that refuses to
clone, so it stops right after the prerequisite step."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "install-bootstrap.sh"


def _run(tmp_path: Path, agents: tuple[str, ...]) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", *agents):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\nexit 1\n" if name == "git" else "#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin"}
    return subprocess.run(["bash", str(BOOTSTRAP)], capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=60)


def test_a_copilot_only_machine_passes_the_prerequisite_check(tmp_path):
    done = _run(tmp_path, ("copilot",))
    assert "claude not found — pick copilot or droid with: bin/assistant-llm.py set <agent>" in done.stderr
    assert "copilot found (GitHub Copilot available as opt-in)" in done.stdout
    assert "no coding agent found" not in done.stderr


def test_a_machine_with_no_agent_stops_and_names_all_three(tmp_path):
    done = _run(tmp_path, ())
    assert done.returncode != 0
    assert ("no coding agent found — install Claude Code (default), GitHub Copilot CLI, "
            "or Factory Droid first") in done.stderr
