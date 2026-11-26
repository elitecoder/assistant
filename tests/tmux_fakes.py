"""A private tmux server for a test, and a stand-in for the `claude` program inside it.

`server()` gives a test its own tmux server: TMUX_TMPDIR points at a new folder, so `tmux` and
bin/tmux_backend.py both reach that server and never the developer's. The folder is under /tmp because a
socket's path must stay under about a hundred bytes, and pytest's own temporary folders are longer than that
on a Mac.

`FakeClaude` stands in for Claude Code in two ways. `agent(server, ref)` starts a process named `claude` in
a pane, as its foreground job, and returns its process id: that is what `ps` and the world scanner look for.
`install()` writes a `claude` program whose `agents --json` prints the rows the test gave it, and points
CLAUDE_BIN at it: that is what the backend asks for the live sessions.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "bin" / "tmux_backend.py"
_RUN = subprocess.run  # the real one, whatever a test puts in its place
TMUX = shutil.which("tmux")  # found once: a test may shorten PATH
needs_tmux = pytest.mark.skipif(TMUX is None, reason="tmux is not installed")


def until(check, timeout: float = 10.0, every: float = 0.05):
    """`check()`'s first truthy answer, asked until `timeout`; fails the test when there is none."""
    deadline = time.monotonic() + timeout
    while True:
        answer = check()
        if answer:
            return answer
        if time.monotonic() > deadline:
            pytest.fail(f"still waiting after {timeout}s: {check}")
        time.sleep(every)


def tmux(*args: str) -> str:
    return subprocess.run([TMUX, *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def server(monkeypatch):
    """A folder holding a private tmux server's socket; the server is killed when the test ends."""
    base = Path(tempfile.mkdtemp(prefix="atx-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(base))
    monkeypatch.setenv("SHELL", "/bin/sh")  # every pane runs a plain shell with a plain prompt
    monkeypatch.setenv("HISTFILE", "/dev/null")  # and that shell writes no history into the test's home as it ends
    for name in ("TMUX", "TMUX_PANE", "ASSISTANT_TMUX_BIN"):
        monkeypatch.delenv(name, raising=False)
    yield base
    # By its socket, never by the environment: a test may have changed that, and a bare `tmux kill-server`
    # would then end the developer's own sessions.
    _RUN([TMUX, "-S", str(base / f"tmux-{os.getuid()}" / "default"), "kill-server"], capture_output=True)
    for _ in range(40):  # a pane's shell may still be writing as it ends; the folder goes once it is quiet
        shutil.rmtree(base, ignore_errors=True)
        if not base.exists():
            break
        time.sleep(0.05)


class FakeClaude:
    def __init__(self, folder: Path, monkeypatch):
        self.folder = folder
        self.rows_file = folder / "claude-rows.json"
        self.program = folder / "bin" / "claude"
        self.program.parent.mkdir(parents=True, exist_ok=True)
        self.program.write_text(
            f"#!{sys.executable}\nimport pathlib, sys\n"
            "if sys.argv[1:] != ['agents', '--json']:\n    sys.exit(2)\n"
            f"sys.stdout.write(pathlib.Path({str(self.rows_file)!r}).read_text())\n")
        self.program.chmod(0o755)
        self.rows([])
        monkeypatch.setenv("CLAUDE_BIN", str(self.program))

    def rows(self, rows) -> None:
        """What `claude agents --json` prints from now on (a list of rows, or any text)."""
        self.rows_file.write_text(rows if isinstance(rows, str) else json.dumps(rows))

    def row(self, pid: int, session_id: str, **fields) -> dict:
        return {"pid": pid, "sessionId": session_id, "cwd": str(self.folder), "kind": "interactive",
                "startedAt": int(time.time() * 1000), "name": "fixture", "status": "idle", **fields}

    def agent(self, backend, ws_ref: str) -> int:
        """Start a long-lived process named `claude` as the foreground job of `ws_ref`'s pane; its pid."""
        stand_in = self.folder / "agents" / "claude"
        stand_in.parent.mkdir(exist_ok=True)
        # A link run under the name `claude`: `ps` names a process by the path it was started with. On a Mac the
        # link is to `sleep`, started where it is (a copy of a system program is killed, and a Python built as a
        # framework starts itself again under its own name). Elsewhere it is to this Python: on some Linux
        # systems `sleep` is one multi-call program that refuses a name it doesn't know.
        on_mac = sys.platform == "darwin"
        if not stand_in.is_symlink():
            stand_in.symlink_to(shutil.which("sleep") if on_mac else sys.executable)
        wait = "600" if on_mac else '-c "import time; time.sleep(600)"'
        pid_file = self.folder / f"pid-{ws_ref.replace(':', '-')}"
        backend(["send", "--workspace", ws_ref, f"sh -c 'echo $$ > {pid_file}; exec {stand_in} {wait}'"])
        backend(["send-key", "--workspace", ws_ref, "enter"])
        until(lambda: pid_file.exists() and pid_file.read_text().strip())
        pid = int(pid_file.read_text())
        until(lambda: subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True,
                                     text=True).stdout.strip().endswith("claude"))
        return pid
