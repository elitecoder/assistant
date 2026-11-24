"""The dashboard server counts a failed page refresh instead of swallowing it."""
import json
import subprocess
import sys

import pytest

from assistant import failwatch
from test_todo_server import mod


def script(tmp_path, body):
    path = tmp_path / "render.py"
    path.write_text(body)
    return path


def rows():
    return list(json.loads(failwatch._store_path().read_text()).values())


def test_a_nonzero_exit_is_counted_with_the_last_line_of_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "RENDER_SCRIPT", script(
        tmp_path, "import sys\nprint('Traceback', file=sys.stderr)\nprint('KeyError: boom 42', file=sys.stderr)\nsys.exit(1)\n"))
    mod.rerender()
    mod.rerender()
    (row,) = rows()
    assert row["component"] == "dashboard-render" and row["count"] == 2
    assert row["sample"] == "KeyError: boom 42" and row["backoff_until"] == 0


def test_a_silent_nonzero_exit_still_says_what_happened(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "RENDER_SCRIPT", script(tmp_path, "import sys\nsys.exit(7)\n"))
    mod.rerender()
    assert rows()[0]["sample"] == "the page render exited with code 7"


def test_the_render_runs_under_the_running_interpreter(tmp_path, monkeypatch):
    seen = []
    real = subprocess.run
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or real([sys.executable, "-c", ""], **kw))
    monkeypatch.setattr(mod, "RENDER_SCRIPT", tmp_path / "x.py")
    mod.rerender()
    mod.rerender_dashboard()
    assert [cmd[0] for cmd, _ in seen] == [sys.executable, sys.executable]
    assert seen[0][1]["timeout"] == 4


def test_a_timeout_is_counted(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(mod.subprocess, "run", slow)
    mod.rerender()
    assert rows()[0]["sample"] == "the page render took longer than 4 seconds"


def test_a_render_that_cannot_start_is_counted(monkeypatch):
    def broken(cmd, **kw):
        raise OSError("no such interpreter")

    monkeypatch.setattr(mod.subprocess, "run", broken)
    mod.rerender()
    assert rows()[0]["sample"] == "the page render could not start: no such interpreter"


def test_success_clears_the_streak_but_keeps_the_count(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "RENDER_SCRIPT", script(tmp_path, "import sys\nsys.exit(1)\n"))
    mod.rerender()
    monkeypatch.setattr(mod, "RENDER_SCRIPT", script(tmp_path, "pass\n"))
    mod.rerender()
    (row,) = rows()
    assert row["count"] == 1 and row["streak"] == 0


def test_the_server_still_answers_when_counting_breaks(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("store is broken")

    monkeypatch.setattr(failwatch, "record_failure", boom)
    monkeypatch.setattr(failwatch, "record_ok", boom)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "x"))
    mod.rerender()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    mod.rerender()


def test_counting_loads_the_package_when_src_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(mod.SRC_DIR)])
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "late"))
    mod.rerender()
    assert rows()[0]["sample"] == "late" and str(mod.SRC_DIR) in sys.path


def test_a_request_that_refreshes_the_page_still_succeeds_when_the_render_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "JSON_PATH", tmp_path / "todo.json")
    (tmp_path / "todo.json").write_text(json.dumps({"items": [{"id": "td-1"}]}))
    monkeypatch.setattr(mod, "RENDER_SCRIPT", script(tmp_path, "import sys\nsys.exit(1)\n"))
    assert mod.set_flag("td-1", "done", True) == (True, "td-1.done = True")
    assert rows()[0]["count"] == 1
