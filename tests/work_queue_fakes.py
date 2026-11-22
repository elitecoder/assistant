"""Fake command-line tools for the Work queue tests: gh, gog, npx, uv, and claude, plus a fake triage model.

Each fake tool logs its arguments to <bin>/<name>.log and answers with the first
response whose key appears in its joined arguments. A response is JSON to
print, a string to print as is, {"exit": n, "stdout": "...", "stderr": "..."},
or {"seq": [...]} to answer each call with the next response.

`Model` stands in for triage.call_model in-process; `fake_claude` puts a fake
`claude` CLI where the real call looks for it, in the temporary HOME.
"""
from __future__ import annotations

import functools
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from work_queue import triage

FENCE = re.compile(r"\nBEGIN UNTRUSTED ROWS ([0-9a-f]{16})\n(.*)\nEND UNTRUSTED ROWS \1\n\Z", re.S)
VERDICT = {"why": "Ana is waiting on your review.", "disposition": "you", "effort": "Small", "draft": ""}


def gh_pr(n: int, title: str = "") -> dict:
    """One pull request as `gh search prs --json` prints it."""
    return {"number": n, "title": title or f"Change {n}", "url": f"https://github.com/o/r/pull/{n}",
            "repository": {"nameWithOwner": "o/r"}, "author": {"login": "ana"}, "createdAt": "2026-11-02T10:00:00Z",
            "isDraft": False}


def payload(prompt: str) -> list[dict]:
    return json.loads(FENCE.search(prompt).group(2))


def kind(prompt: str) -> str:
    return next(k for k, rubric in (("rows", triage.ROWS_RUBRIC), ("start", triage.START_RUBRIC),
                                    ("changes", triage.CHANGES_RUBRIC)) if prompt.startswith(rubric))


class Model:
    """A fake model: answers each kind of prompt from a function of its rows, and keeps every prompt."""

    def __init__(self, rows=None, start=None, changes=None):
        self.prompts: list[str] = []
        self.answer = {
            "rows": rows or (lambda items: {"rows": [{"id": i["id"], **VERDICT} for i in items]}),
            "start": start or (lambda items: {"start": [{"id": i["id"], "reason": f"Start with {i['title']}"} for i in items[:6]]}),
            "changes": changes or (lambda items: {"summary": [{"source": i["source"], "text": f"{i['source']} moved."} for i in items]}),
        }

    def __call__(self, prompt: str, timeout: int) -> str:
        self.prompts.append(prompt)
        found = self.answer[kind(prompt)](payload(prompt))
        return found if isinstance(found, str) else "Here you go:\n" + json.dumps(found)

    def of(self, name: str) -> list[list[dict]]:
        return [payload(p) for p in self.prompts if kind(p) == name]


def raises(message: str):
    """A Model answer that fails the way the real call does."""
    def call(items):
        raise triage.Unavailable(message)
    return call


CLAUDE = '''#!{python}
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
home = Path(os.environ["HOME"])
with open(home / "claude.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
slow = home / "claude.slow"
if slow.exists() and prompt.startswith(slow.read_text()):
    import time
    time.sleep(30)
data = json.loads(prompt.split("\\n")[-3])
if "\\nBEGIN UNTRUSTED ROWS " not in prompt:
    answer = {{}}
elif prompt.startswith("You triage"):
    answer = {{"rows": [{{"id": r["id"], "why": "A fake verdict.", "disposition": "agent", "effort": "Quick"}} for r in data]}}
elif prompt.startswith("You pick"):
    answer = {{"start": [{{"id": r["id"], "reason": "A fake reason."}} for r in data]}}
else:
    answer = {{"summary": [{{"source": r["source"], "text": "A fake summary."}} for r in data]}}
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(answer),
                  "usage": {{"input_tokens": 100, "output_tokens": 20}}, "total_cost_usd": 0.001}}))
'''


def fake_claude(home: Path) -> Path:
    """A fake `claude` at ~/.local/bin/claude that logs each call to ~/claude.log and answers every prompt kind. A prompt
    that starts with the text in ~/claude.slow gets no answer for 30 seconds."""
    exe = home / ".local/bin/claude"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(CLAUDE.format(python=sys.executable))
    exe.chmod(0o755)
    return home / "claude.log"

MODEL_ENV = ("ASSISTANT_LLM_CONFIG", "CLAUDE_BIN", "COPILOT_BIN", "DROID_BIN", "WORK_QUEUE_LLM_PROVIDER",
             "WORK_QUEUE_DROID_MODEL", "WORK_QUEUE_LOCAL_MODEL", "WORK_QUEUE_COPILOT_MODEL", "COPILOT_MODEL",
             "ASSISTANT_LOCAL_LLM_URL", "ASSISTANT_MODEL_BALANCED", "MODEL_PROVIDER", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX")
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


OLD_PYTHON = '''#!{python}
import os, runpy, sys
args = sys.argv[1:]
if args[:1] not in (["-c"], ["-m"]):
    os.execv(sys.executable, [sys.executable, *args])
sys.version_info = {version!r} + ("final", 0)
sys.version = "{dotted} (an older Python, for tests)"
if args[0] == "-c":
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {{"__name__": "__main__"}})
else:
    sys.argv = [args[1], *args[2:]]
    sys.path.insert(0, os.getcwd())
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
'''


def old_python(where: Path, version: tuple = (3, 10, 14)) -> Path:
    """This interpreter, reporting an older version in sys.version_info and sys.version. It runs `-c` code and `-m`
    modules itself, and anything else on the real interpreter."""
    where.mkdir(parents=True, exist_ok=True)
    exe = where / f"python{version[0]}.{version[1]}"
    exe.write_text(OLD_PYTHON.format(python=sys.executable, version=version, dotted=".".join(map(str, version))))
    exe.chmod(0o755)
    return exe


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


def no_model(prompt: str, timeout: int) -> str:
    raise triage.Unavailable("the tests have no model")


@pytest.fixture
def wq(tmp_path, monkeypatch) -> Env:
    """A Work queue with its own HOME (and so its own TODO store), data folder, and fake tools.

    Triage's model call fails unless a test passes its own fake, and nothing in the
    environment can route a call to a real model."""
    env = Env(tmp_path)
    env.todo.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("WQ_DATA", str(env.data))
    for name in ("SLACK_USER_TOKEN", "GH_HOST", "CMUX_BIN", *MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(triage, "call_model", no_model)
    env.todo.write_text(json.dumps({"items": [{"id": "td-041", "status": "open"}], "completed": [{"id": "td-050"}],
                                    "removed": []}))
    use_fakes(monkeypatch, env.bin)
    return env
