"""commands/close-workspace.py — `/close-workspace` closes the workspace
whose terminal runs it, or nothing.

Runs the real script as a subprocess against a fake `cmux` that serves a
`top --processes` document and records every call. This test process stands in
for a terminal's login process: the script is its child, so the lineage check
walks the real process table.

The 2026-11-15 regression: a session with no CMUX_WORKSPACE_ID fell back to
`cmux current-workspace` and closed whatever workspace was on screen, twice.
A stale CMUX_WORKSPACE_ID closed another workspace the same way.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "commands" / "close-workspace.py"
COMMAND = REPO / "commands" / "close-workspace.md"

A = "AAAAAAAA-0000-0000-0000-00000000000A"
B = "BBBBBBBB-0000-0000-0000-00000000000B"
APP = os.getppid()
ME = os.getpid()

FAKE = """#!{python}
import json, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
with open(here / "argv.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:5] == ["--json", "--id-format", "both", "top", "--all"]:
    name = "top"
    if (here / "break-after-top").exists():
        pathlib.Path(__file__).chmod(0o644)
elif args[:1] == ["close-workspace"]:
    name = "close-force" if "--force" in args else "close"
else:
    sys.exit(97)
sys.stdout.write((here / f"{{name}}.out").read_text())
sys.stderr.write((here / f"{{name}}.err").read_text())
sys.exit(int((here / f"{{name}}.rc").read_text()))
"""


def proc(pid, ppid, reason="explicit-root-pid", name="login"):
    return {"pid": pid, "ppid": ppid, "name": name, "attribution_reason": reason, "children": []}


def workspace(uuid, ref, *processes, pinned=False, selected=False, title="work"):
    return {
        "id": uuid, "ref": ref, "title": title, "pinned": pinned, "selected": selected,
        "panes": [{"surfaces": [{"ref": ref.replace("workspace", "surface"), "processes": list(processes)}]}],
    }


def top(*workspaces, app=(APP,)):
    return {"windows": [{"ref": "window:1", "app_process_pids": list(app), "workspaces": list(workspaces)}]}


# Starts the rest of argv in a new session, as a detached background process does.
NEW_SESSION = [sys.executable, "-c",
               "import os, subprocess, sys; os.setsid(); sys.exit(subprocess.call(sys.argv[1:]))"]
# Keeps a shell between the caller and the rest of argv.
VIA_SHELL = ["/bin/sh", "-c", '"$@"; exit $?', "sh"]

# The terminal this test runs in is workspace:5 (A). workspace:6 (B) is the one
# on screen, which a focus-based fallback would close.
MINE = workspace(A, "workspace:5", proc(ME, APP), title="my task [1]")
ON_SCREEN = workspace(B, "workspace:6", proc(424242, APP), selected=True, title="on screen [0]")


@pytest.fixture
def fake(tmp_path):
    cli = tmp_path / "cmux"
    cli.write_text(FAKE.format(python=sys.executable))
    cli.chmod(0o755)

    def serve(name, out="", err="", rc=0):
        (tmp_path / f"{name}.out").write_text(out)
        (tmp_path / f"{name}.err").write_text(err)
        (tmp_path / f"{name}.rc").write_text(str(rc))

    serve("top", json.dumps(top(MINE, ON_SCREEN)))
    serve("close", "OK workspace:5\n")
    serve("close-force", "OK workspace:5\n")

    def run(*args, workspace_id=A, via=(), path_first=None, cmux=cli):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CMUX_")}
        env["CMUX_BIN"] = str(cmux)
        if workspace_id is not None:
            env["CMUX_WORKSPACE_ID"] = workspace_id
        if path_first:
            env["PATH"] = f"{path_first}{os.pathsep}{env['PATH']}"
        res = subprocess.run([*via, sys.executable, str(SCRIPT), *args], capture_output=True,
                             text=True, env=env, timeout=30)
        log = tmp_path / "argv.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return res, calls

    run.serve = serve
    return run


def closes(calls):
    return [c for c in calls if c[:1] == ["close-workspace"]]


def test_closes_the_workspace_whose_terminal_runs_it(fake):
    res, calls = fake()
    assert res.returncode == 0, res.stdout
    assert closes(calls) == [["close-workspace", "--workspace", A]]
    assert "Closed workspace:5" in res.stdout


def test_closes_by_lineage_when_workspace_id_is_missing(fake):
    res, calls = fake(workspace_id=None)
    assert res.returncode == 0, res.stdout
    assert closes(calls) == [["close-workspace", "--workspace", A]]


def test_never_asks_cmux_for_the_selected_workspace(fake):
    unmatched = top(workspace(A, "workspace:5", proc(424243, APP)), ON_SCREEN)
    for document in (top(MINE, ON_SCREEN), unmatched):
        fake.serve("top", json.dumps(document))
        for workspace_id in (None, A, B, ""):
            _, calls = fake(workspace_id=workspace_id)
            used = {c[0] if c[0] != "--json" else c[3] for c in calls}
            assert used <= {"top", "close-workspace"}, calls


def test_matches_the_workspace_id_in_any_case(fake):
    res, calls = fake(workspace_id=A.lower())
    assert res.returncode == 0, res.stdout
    assert closes(calls) == [["close-workspace", "--workspace", A]]


def test_finds_the_terminal_through_intermediate_processes(fake):
    res, calls = fake(via=[*VIA_SHELL, *VIA_SHELL])
    assert res.returncode == 0, res.stdout
    assert closes(calls) == [["close-workspace", "--workspace", A]]


def test_allows_the_one_detached_session_the_command_runs_in(fake):
    res, calls = fake(via=NEW_SESSION)
    assert res.returncode == 0, res.stdout
    assert closes(calls) == [["close-workspace", "--workspace", A]]


def test_refuses_when_a_background_session_sits_between_it_and_the_terminal(fake):
    res, calls = fake(via=[*NEW_SESSION, *NEW_SESSION])
    assert res.returncode == 1
    assert closes(calls) == []
    assert "background process" in res.stdout


def test_refuses_when_workspace_id_names_another_workspace(fake):
    res, calls = fake(workspace_id=B)
    assert res.returncode == 1
    assert closes(calls) == []
    assert B in res.stdout and "workspace:5" in res.stdout


def test_refuses_when_no_terminal_login_is_an_ancestor(fake):
    fake.serve("top", json.dumps(top(workspace(A, "workspace:5", proc(424243, APP)), ON_SCREEN)))
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == []
    assert "isn't running in a cmux terminal" in res.stdout


def test_ignores_processes_cmux_matches_by_environment(fake):
    by_env = proc(ME, APP, reason="cmux-environment", name="claude")
    fake.serve("top", json.dumps(top(workspace(A, "workspace:5", by_env, proc(424243, APP)), ON_SCREEN)))
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == []


def test_ignores_an_orphan_that_still_holds_the_terminal(fake):
    orphan = proc(ME, 1, name="sh")
    fake.serve("top", json.dumps(top(workspace(A, "workspace:5", proc(424243, APP), orphan), ON_SCREEN)))
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == []


def test_refuses_a_pinned_workspace(fake):
    fake.serve("top", json.dumps(top(workspace(A, "workspace:5", proc(ME, APP), pinned=True), ON_SCREEN)))
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == []
    assert "pinned" in res.stdout


def test_refuses_when_two_workspaces_claim_the_terminal(fake):
    fake.serve("top", json.dumps(top(MINE, workspace(B, "workspace:6", proc(ME, APP)))))
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == []
    assert "workspace:5" in res.stdout and "workspace:6" in res.stdout


@pytest.mark.parametrize("out,rc", [("", 1), ("not json", 0), ("{}", 0)])
def test_refuses_when_cmux_cannot_list_terminals(fake, out, rc):
    fake.serve("top", out, err="socket closed", rc=rc)
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == []
    assert "Didn't close any workspace" in res.stdout


def test_reports_a_missing_cmux_without_a_traceback(fake, tmp_path):
    res, calls = fake(cmux=tmp_path / "no-cmux-here")
    assert res.returncode == 1
    assert res.stdout.startswith("Didn't close any workspace")
    assert "Traceback" not in res.stderr


def test_reports_a_failed_process_listing_without_a_traceback(fake, tmp_path):
    bin_dir = tmp_path / "psbin"
    bin_dir.mkdir()
    (bin_dir / "ps").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "ps").chmod(0o755)
    res, calls = fake(path_first=bin_dir)
    assert res.returncode == 1
    assert res.stdout.startswith("Didn't close any workspace")
    assert closes(calls) == []


def test_forces_the_same_workspace_when_cmux_asks_to_confirm(fake):
    fake.serve("close", err="Error: confirmation_required: Workspace has a running process; "
                            "retry with --force", rc=1)
    res, calls = fake()
    assert res.returncode == 0, res.stdout
    assert closes(calls) == [["close-workspace", "--workspace", A],
                             ["close-workspace", "--workspace", A, "--force"]]


def test_reports_when_cmux_cannot_be_run_for_the_close(fake, tmp_path):
    (tmp_path / "break-after-top").write_text("")
    res, calls = fake()
    assert res.returncode == 1
    assert "didn't answer" in res.stdout and "Check whether it's still open" in res.stdout
    assert closes(calls) == []


def test_reports_a_failed_close_without_trying_another_target(fake):
    fake.serve("close", err="Error: protected: Pinned workspaces can't be closed while pinned.", rc=1)
    res, calls = fake()
    assert res.returncode == 1
    assert closes(calls) == [["close-workspace", "--workspace", A]]
    assert "protected" in res.stdout


def test_warns_when_cmux_reports_closing_a_different_workspace(fake):
    fake.serve("close", "OK workspace:6\n")
    res, calls = fake()
    assert res.returncode == 2
    assert "Warning" in res.stdout


def test_dry_run_names_the_target_and_closes_nothing(fake):
    res, calls = fake("--dry-run", workspace_id=None)
    assert res.returncode == 0
    assert closes(calls) == []
    assert f"Would close workspace:5 ({A})" in res.stdout


def test_main_in_process_finds_the_same_target(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("CMUX_BIN", str(tmp_path / "cmux"))
    monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
    spec = importlib.util.spec_from_file_location("close_workspace", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--dry-run"]) == 0
    assert f"Would close workspace:5 ({A})" in capsys.readouterr().out


def test_the_command_runs_the_script_before_the_model_gets_a_turn():
    text = COMMAND.read_text()
    front, body = text.split("---\n")[1:3]
    assert "disable-model-invocation: true" in front
    assert "!`python3 ~/.claude/commands/close-workspace.py`" in body
    assert "allowed-tools: Bash(python3 ~/.claude/commands/close-workspace.py)" in front
