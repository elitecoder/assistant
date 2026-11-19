"""Fake command-line tools for the Work queue tests: gh, gog, npx, and uv.

Each fake logs its arguments to <bin>/<name>.log and answers with the first
response whose key appears in its joined arguments. A response is JSON to
print, a string to print as is, {"exit": n, "stdout": "...", "stderr": "..."},
or {"seq": [...]} to answer each call with the next response.
"""
from __future__ import annotations

import functools
import json
import os
import pwd
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = '''#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
args = " ".join(sys.argv[1:])
with open(here / "{name}.log", "a") as log:
    log.write(args + "\\n")
with open(here / "{name}.env", "a") as log:
    log.write(os.environ.get("GH_HOST", "") + "\\n")
for n, (key, answer) in enumerate(json.loads((here / "{name}.json").read_text())):
    if key in args:
        if isinstance(answer, dict) and "seq" in answer:
            count = here / "{name}.{{}}.count".format(n)
            seen = int(count.read_text()) if count.exists() else 0
            count.write_text(str(seen + 1))
            answer = answer["seq"][min(seen, len(answer["seq"]) - 1)]
        if isinstance(answer, dict) and "exit" in answer:
            sys.stdout.write(answer.get("stdout", ""))
            sys.stderr.write(answer.get("stderr", ""))
            sys.exit(answer["exit"])
        sys.stdout.write(answer if isinstance(answer, str) else json.dumps(answer))
        sys.exit(0)
sys.stderr.write("fake {name}: no answer for " + args)
sys.exit(9)
'''


def fake_tool(bin_dir: Path, name: str, answers: list[tuple[str, object]]) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / f"{name}.json").write_text(json.dumps(answers))
    script = bin_dir / name
    script.write_text(SCRIPT.format(python=sys.executable, name=name))
    script.chmod(0o755)
    return script


def calls(bin_dir: Path, name: str) -> list[str]:
    log = bin_dir / f"{name}.log"
    return log.read_text().splitlines() if log.exists() else []


def use_fakes(monkeypatch, bin_dir: Path) -> None:
    """Put only the fakes and the system basics on PATH, so a real tool can't run."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))


def real_browsers(monkeypatch) -> None:
    """Find Playwright's browsers in the real home, even when a test or the mutation check moves HOME."""
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                       or str(real_home / "Library/Caches/ms-playwright"))


@functools.cache
def chromium_missing(browsers_path: str) -> str:
    """Why Playwright can't start Chromium from browsers_path because it isn't installed; "" when it starts."""
    from playwright.sync_api import Error, sync_playwright

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Error as exc:
        if "Executable doesn't exist" in str(exc):
            return str(exc).strip().splitlines()[0]
        raise
    return ""


def require_chromium(monkeypatch) -> None:
    """Skip the test, instead of failing it, when Playwright's Chromium isn't installed."""
    real_browsers(monkeypatch)
    if missing := chromium_missing(os.environ["PLAYWRIGHT_BROWSERS_PATH"]):
        pytest.skip(f"Playwright's Chromium isn't installed. Run python -m playwright install chromium ({missing})")


def spare_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def first_line(proc: subprocess.Popen, seconds: float = 30) -> str:
    """The process's first line of output; kills it and fails the test when none comes in time."""
    got: list[str] = []
    reader = threading.Thread(target=lambda: got.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(seconds)
    if not got:
        proc.kill()
        pytest.fail(f"{proc.args} printed nothing within {seconds} seconds")
    return got[0]


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.data = root / "data"
        self.bin = root / "fakebin"
        self.todo = self.home / ".claude/assistant-todo.json"

    def sources(self, doc: dict) -> None:
        path = self.home / ".assistant/work-queue/sources.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def saved_sources(self) -> dict:
        return json.loads((self.home / ".assistant/work-queue/sources.json").read_text())

    def items(self) -> list[dict]:
        return json.loads(self.todo.read_text())["items"]


@pytest.fixture
def wq(tmp_path, monkeypatch) -> Env:
    """A Work queue with its own HOME (and so its own TODO store), data folder, and fake tools."""
    env = Env(tmp_path)
    env.todo.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("WQ_DATA", str(env.data))
    for name in ("SLACK_USER_TOKEN", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    env.todo.write_text(json.dumps({"items": [{"id": "td-041", "status": "open"}], "completed": [{"id": "td-050"}],
                                    "removed": []}))
    use_fakes(monkeypatch, env.bin)
    return env
