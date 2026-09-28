"""Tests for bin/run-pulse.py — the launchd pre-flight for the pulse.

In-process tests call the real module with a fake `execv` so the parse check,
the logged skip, and the exact exec command are measured. Subprocess tests copy
the real file next to a stub pulse and let it exec for real.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import plistlib
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


def _layout(tmp: Path, pulse_body: str, model_tiers: str = "TIERS = {}\n") -> Path:
    (tmp / "bin").mkdir()
    shutil.copy2(WRAPPER, tmp / "bin/run-pulse.py")
    (tmp / "bin/pulse.py").write_text(pulse_body)
    (tmp / "src/assistant").mkdir(parents=True)
    (tmp / "src/assistant/__init__.py").write_text("")
    (tmp / "src/assistant/model_tiers.py").write_text(model_tiers)
    return tmp / "bin/run-pulse.py"


def test_healthy_checkout_clears_old_failure_and_execs_pulse(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    record = tmp_path / ".assistant/pulse-preflight.json"
    record.parent.mkdir()
    record.write_text('{"failed_at": 1, "error": "old"}')
    mod = _load()
    calls = []
    assert mod.main(["--pulse-idx", "5"], execv=lambda *a: calls.append(a)) == 0
    assert calls == [(sys.executable, [sys.executable, str(REPO / "bin/pulse.py"),
                                       "--pulse-idx", "5"])]
    assert not record.exists()


def test_broken_pulse_skips_the_run_and_records_why(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _layout(tmp_path, "def broken(:\n    pass\n")
    mod = _load()
    calls = []
    with mock.patch.object(mod, "STARTUP_FILES", (tmp_path / "bin/pulse.py",)):
        assert mod.main([], execv=lambda *a: calls.append(a)) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "pulse pre-flight FAILED, skipping this run" in err
    assert f"{tmp_path / 'bin/pulse.py'}: SyntaxError" in err
    record = json.loads((tmp_path / "home/.assistant/pulse-preflight.json").read_text())
    assert record["error"].startswith(f"{tmp_path / 'bin/pulse.py'}: SyntaxError")
    assert record["ledgered_at"] == record["failed_at"]
    [entry] = [json.loads(line) for line in
               (tmp_path / "home/.assistant/actions-ledger.jsonl").read_text().splitlines()]
    assert entry["kind"] == "pulse-preflight-fail"
    assert entry["outcome"] == "failed"
    assert entry["evidence"] == f"pulse can't start: {record['error']}"[:300]


def test_ledger_entry_is_written_for_a_new_error_then_once_a_day(tmp_path):
    mod = _load()
    ledger = tmp_path / "actions-ledger.jsonl"

    def entries():
        return [json.loads(line)["evidence"] for line in ledger.read_text().splitlines()]

    mod._record_failure(tmp_path, "err A", 1000.0)
    mod._record_failure(tmp_path, "err A", 1000.0 + 3600)
    assert entries() == ["pulse can't start: err A"]
    mod._record_failure(tmp_path, "err B", 1000.0 + 7200)
    mod._record_failure(tmp_path, "err B", 1000.0 + 7200 + 86400)
    assert entries() == ["pulse can't start: err A", "pulse can't start: err B",
                         "pulse can't start: err B"]
    record = json.loads((tmp_path / "pulse-preflight.json").read_text())
    assert record == {"failed_at": 1000.0 + 7200 + 86400, "error": "err B",
                      "ledgered_at": 1000.0 + 7200 + 86400}


def test_unwritable_record_still_skips_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home/.assistant").write_text("a file where the folder should be")
    _layout(tmp_path, "def broken(:\n")
    mod = _load()
    calls = []
    with mock.patch.object(mod, "STARTUP_FILES", (tmp_path / "bin/pulse.py",)):
        assert mod.main([], execv=lambda *a: calls.append(a)) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "could not record the failure" in err
    assert "pulse pre-flight FAILED, skipping this run" in err


def test_broken_startup_import_skips_the_run(tmp_path):
    wrapper = _layout(tmp_path, 'print("RAN")\n', model_tiers="x = 1\n<<<<<<< HEAD\n")
    r = subprocess.run([sys.executable, str(wrapper)], capture_output=True, text=True,
                       timeout=60, env=dict(os.environ, HOME=str(tmp_path / "home")))
    assert r.returncode == 0
    assert r.stdout == ""
    assert "src/assistant/model_tiers.py: SyntaxError" in r.stderr


def test_missing_startup_file_skips_the_run(tmp_path):
    wrapper = _layout(tmp_path, 'print("RAN")\n')
    (tmp_path / "src/assistant/model_tiers.py").rename(tmp_path / "src/assistant/moved.py")
    r = subprocess.run([sys.executable, str(wrapper)], capture_output=True, text=True,
                       timeout=60, env=dict(os.environ, HOME=str(tmp_path / "home")))
    assert r.returncode == 0
    assert r.stdout == ""
    assert "model_tiers.py: FileNotFoundError" in r.stderr


def test_broken_later_module_does_not_block_the_pulse(tmp_path):
    # The pulse guards the modules it loads later; an unrelated broken module
    # must not stop every run.
    wrapper = _layout(tmp_path, 'print("RAN")\n')
    (tmp_path / "src/assistant/narrator.py").write_text("def broken(:\n")
    r = subprocess.run([sys.executable, str(wrapper)], capture_output=True, text=True,
                       timeout=60, env=dict(os.environ, HOME=str(tmp_path / "home")))
    assert r.returncode == 0
    assert r.stdout.strip() == "RAN"


def _module_level_imports(nodes):
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        for field in ("body", "orelse", "finalbody", "handlers"):
            yield from _module_level_imports(getattr(node, field, []))


def test_startup_files_match_pulse_module_level_imports():
    tree = ast.parse((REPO / "bin/pulse.py").read_text())
    imported = {"bin/pulse.py"}
    for node in _module_level_imports(tree.body):
        modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                   else [node.module])
        for module in modules:
            top = module.split(".")[0]
            if top in sys.stdlib_module_names or top == "__future__":
                continue
            assert module == "assistant", f"add {module} to run-pulse.py STARTUP_FILES"
            imported.add("src/assistant/__init__.py")
            imported.update(f"src/assistant/{alias.name}.py" for alias in node.names)
    listed = {str(path.relative_to(REPO)) for path in _load().STARTUP_FILES}
    assert imported == listed


def test_script_entry_point_execs_pulse(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    with mock.patch.object(os, "execv", lambda *a: calls.append(a)), \
            mock.patch.object(sys, "argv", [str(WRAPPER), "--dry-run"]):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(WRAPPER), run_name="__main__")
    assert exc.value.code == 0
    assert calls == [(sys.executable, [sys.executable, str(REPO / "bin/pulse.py"),
                                       "--dry-run"])]


def test_real_exec_runs_pulse_and_passes_args(tmp_path):
    wrapper = _layout(tmp_path, 'import sys\nprint("RAN", " ".join(sys.argv[1:]))\n')
    r = subprocess.run([sys.executable, str(wrapper), "--pulse-idx", "5"],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, HOME=str(tmp_path / "home")))
    assert r.returncode == 0
    assert r.stdout.strip() == "RAN --pulse-idx 5"
    assert r.stderr == ""


def test_real_run_with_broken_pulse_exits_zero(tmp_path):
    wrapper = _layout(tmp_path, "def broken(:\n")
    r = subprocess.run([sys.executable, str(wrapper)],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, HOME=str(tmp_path / "home")))
    assert r.returncode == 0
    assert r.stdout == ""
    assert "pulse pre-flight FAILED" in r.stderr


def test_pulse_launch_agent_runs_the_preflight():
    plist = plistlib.loads((REPO / "launchagents/com.assistant.assistant-pulse.plist")
                           .read_bytes())
    assert plist["ProgramArguments"] == ["__PYTHON__", "__REPO__/bin/run-pulse.py"]
