"""hooks/cmux-auto-resume.py forwards SessionStart to cmux, confirms the record
landed, retries with backoff, ledgers a final failure, and sets a fallback
resume binding — in a detached worker, so SessionStart never waits on cmux.

cmux is a fake executable written into the test's tmp dir. It answers
`hooks <provider> session-start` (exit codes from session-start.rc, one per
call; records the session unless no-land exists), `sessions list --session`
(other-session makes it list only someone else's session),
and `surface resume set`, logging every call to calls.jsonl.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "cmux-auto-resume.py"
# Lets a coverage run measure the hook's own subprocesses.
COVERAGE_ENV = {k: v for k, v in os.environ.items() if k.startswith("COVERAGE")}

FAKE_CMUX = """#!{python}
import json, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
stdin = sys.stdin.read() if args[:1] == ["hooks"] else ""
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps({{"args": args, "stdin": stdin}}) + "\\n")
recorded = here / "recorded.json"
sessions = json.loads(recorded.read_text()) if recorded.exists() else []
if args[:1] == ["hooks"] and args[2:3] == ["session-start"]:
    if (here / "slow").exists():
        time.sleep(float((here / "slow").read_text() or 3))
    codes = (here / "session-start.rc").read_text().split() if (here / "session-start.rc").exists() else []
    code = int(codes.pop(0)) if codes else 0
    (here / "session-start.rc").write_text(" ".join(codes))
    if code == 0 and not (here / "no-land").exists():
        sessions.append({{"session_id": json.loads(stdin)["session_id"]}})
        recorded.write_text(json.dumps(sessions))
    (here / "session-start.done").write_text("")
    sys.exit(code)
if args[:2] == ["sessions", "list"]:
    if (here / "junk-list").exists():
        print("not json")
        sys.exit(0)
    sid = args[args.index("--session") + 1]
    if (here / "other-session").exists():
        sessions, sid = [{{"session_id": "someone-else"}}], "someone-else"
    print(json.dumps({{"sessions": [s for s in sessions if s["session_id"] == sid]}}))
    sys.exit(0)
if args[:3] == ["surface", "resume", "set"]:
    sys.exit(0)
sys.exit(2)
"""


def load_module():
    spec = importlib.util.spec_from_file_location("cmux_auto_resume_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cmux(tmp_path):
    directory = tmp_path / "cmux-bin"
    directory.mkdir()
    cli = directory / "cmux"
    cli.write_text(FAKE_CMUX.format(python=sys.executable))
    cli.chmod(0o755)
    return directory


def calls(directory):
    log = directory / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def environment(tmp_path, cmux, **extra):
    return {"HOME": str(tmp_path), "CMUX_SURFACE_ID": "surface-1",
            "CMUX_WORKSPACE_ID": "workspace-1",
            "CMUX_BUNDLED_CLI_PATH": str(cmux / "cmux"), **extra}


def ledger(tmp_path):
    path = tmp_path / ".assistant" / "actions-ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_worker(module, provider, payload, sleeps):
    module.worker({"provider": provider, "payload": payload}, sleep=sleeps.append)


def test_factory_forward_is_confirmed_then_binds_a_droid_resume(tmp_path, cmux):
    module = load_module()
    lessons = tmp_path / ".claude/CLAUDE.md"
    lessons.parent.mkdir(parents=True)
    lessons.write_text("rules")
    payload = {"session_id": "factory-session", "cwd": "/tmp/project with space",
               "transcript_path": "/Users/test/.factory/sessions/project/session.jsonl"}
    env = environment(tmp_path, cmux, CMUX_AGENT_LAUNCH_EXECUTABLE="/usr/local/bin/droid")
    sleeps = []
    with mock.patch.dict(os.environ, env, clear=True):
        run_worker(module, "factory", payload, sleeps)

    started, listed, bound = calls(cmux)
    assert started["args"] == ["hooks", "factory", "session-start"]
    assert json.loads(started["stdin"]) == payload
    assert listed["args"] == ["sessions", "list", "--session", "factory-session", "--all", "--json"]
    assert bound["args"][bound["args"].index("--kind") + 1] == "factory"
    assert bound["args"][bound["args"].index("--name") + 1] == "Factory Droid"
    shell = bound["args"][bound["args"].index("--shell") + 1]
    assert "/usr/local/bin/droid" in shell
    assert "--resume factory-session" in shell
    assert "droid-glm-settings.json" in shell
    assert "--auto high" in shell
    assert "--append-system-prompt-file" in shell
    assert sleeps == [0]
    assert ledger(tmp_path) == []


def test_claude_binding_uses_the_launch_executable(tmp_path, cmux):
    module = load_module()
    env = environment(tmp_path, cmux, CMUX_AGENT_LAUNCH_EXECUTABLE="claude-custom")
    with mock.patch.dict(os.environ, env, clear=True):
        run_worker(module, "claude", {"session_id": "claude-session", "cwd": "/tmp/repo"}, [])
    bound = calls(cmux)[-1]["args"]
    assert bound[bound.index("--kind") + 1] == "claude"
    assert bound[bound.index("--name") + 1] == "Claude Code"
    shell = bound[bound.index("--shell") + 1]
    assert "claude-custom --resume claude-session" in shell
    assert "droid-glm-settings.json" not in shell


def test_wrapper_launch_needs_no_fallback_binding(tmp_path, cmux):
    module = load_module()
    env = environment(tmp_path, cmux, CMUX_AGENT_LAUNCH_ARGV_B64="present")
    with mock.patch.dict(os.environ, env, clear=True):
        run_worker(module, "factory", {"session_id": "sid", "cwd": "/tmp/repo"}, [])
    assert [c["args"][:3] for c in calls(cmux)] == [
        ["hooks", "factory", "session-start"], ["sessions", "list", "--session"]]


def test_a_failed_forward_is_retried_with_backoff(tmp_path, cmux):
    module = load_module()
    (cmux / "session-start.rc").write_text("1 75 0")
    sleeps = []
    with mock.patch.dict(os.environ, environment(tmp_path, cmux), clear=True):
        run_worker(module, "claude", {"session_id": "sid", "cwd": "/r"}, sleeps)
    starts = [c for c in calls(cmux) if c["args"][:1] == ["hooks"]]
    assert len(starts) == 3
    assert sleeps == list(module.RETRY_DELAYS_SEC[:3])
    assert ledger(tmp_path) == []


@pytest.mark.parametrize("failure,reason", [
    ("no-land", "cmux has no record of the session after session-start"),
    ("other-session", "cmux has no record of the session after session-start"),
    ("junk-list", "sessions list exit 0: unreadable output"),
    ("rc", "session-start exit 1: "),
])
def test_a_session_cmux_never_records_is_ledgered_after_the_last_try(
        tmp_path, cmux, failure, reason):
    module = load_module()
    if failure == "rc":
        (cmux / "session-start.rc").write_text(" ".join("1" for _ in module.RETRY_DELAYS_SEC))
    else:
        (cmux / failure).write_text("")
    sleeps = []
    with mock.patch.dict(os.environ, environment(tmp_path, cmux), clear=True):
        run_worker(module, "claude", {"session_id": "lost-session", "cwd": "/r"}, sleeps)
    assert sleeps == list(module.RETRY_DELAYS_SEC)
    starts = [c for c in calls(cmux) if c["args"][:1] == ["hooks"]]
    assert len(starts) == len(module.RETRY_DELAYS_SEC)
    assert calls(cmux)[-1]["args"][:3] == ["surface", "resume", "set"]
    [entry] = ledger(tmp_path)
    assert entry["kind"] == "cmux-session-forward-fail"
    assert entry["key"] == "cmux-session-forward-fail:lost-session"
    assert entry["outcome"] == "failed"
    assert entry["evidence"].startswith(
        "cmux never recorded claude session lost-session (workspace workspace-1, "
        f"surface surface-1) after {len(module.RETRY_DELAYS_SEC)} tries: {reason}")


def test_a_missing_or_hung_cli_is_a_reason_not_a_crash(tmp_path, cmux, monkeypatch):
    module = load_module()
    env = environment(tmp_path, cmux, CMUX_BUNDLED_CLI_PATH=str(tmp_path / "no-cmux"))
    with mock.patch.dict(os.environ, env, clear=True):
        run_worker(module, "claude", {"session_id": "sid", "cwd": "/r"}, [])
    assert "session-start failed:" in ledger(tmp_path)[0]["evidence"]

    (cmux / "slow").write_text("")
    monkeypatch.setattr(module, "CALL_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(module, "RETRY_DELAYS_SEC", (0,))
    with mock.patch.dict(os.environ, environment(tmp_path, cmux), clear=True):
        run_worker(module, "claude", {"session_id": "slow-sid", "cwd": "/r"}, [])
    assert ledger(tmp_path)[1]["evidence"].endswith("session-start timed out after 0.5s")

    def broken_list(error):
        def run(args, **kwargs):
            if args[1:3] == ["sessions", "list"]:
                raise error
            return subprocess.CompletedProcess(args, 0, "", "")
        return run

    assert module.forward_once("cmux", "claude", {"session_id": "s"},
                               broken_list(OSError("socket gone"))) == (
        "sessions list failed: socket gone")
    assert module.forward_once("cmux", "claude", {"session_id": "s"},
                               broken_list(subprocess.TimeoutExpired("cmux", 0.5))) == (
        "sessions list timed out after 0.5s")


def test_the_hook_returns_at_once_and_the_detached_worker_finishes(tmp_path, cmux):
    # session-start takes 8 s here; SessionStart must return before it ends.
    # The done marker, not a wall clock, decides, so a loaded machine can't
    # fail it.
    (cmux / "slow").write_text("8")
    payload = {"session_id": "e2e-session", "cwd": str(tmp_path), "provider": "claude"}
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                            capture_output=True, text=True, timeout=60,
                            env={**environment(tmp_path, cmux), **COVERAGE_ENV,
                                 "PATH": os.environ["PATH"]})
    assert not (cmux / "session-start.done").exists()
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and len(calls(cmux)) < 3:
        time.sleep(0.1)
    assert [c["args"][:3] for c in calls(cmux)] == [
        ["hooks", "claude", "session-start"], ["sessions", "list", "--session"],
        ["surface", "resume", "set"]]
    assert ledger(tmp_path) == []


@pytest.mark.parametrize("env,payload", [
    ({}, {"session_id": "sid", "provider": "claude"}),
    ({"CMUX_SURFACE_ID": "surface-1"}, {"provider": "claude"}),
    ({"CMUX_SURFACE_ID": "surface-1"}, {"session_id": "sid", "provider": "codex"}),
])
def test_nothing_starts_outside_cmux_or_without_a_supported_session(env, payload):
    module = load_module()
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))), \
            mock.patch.object(module.subprocess, "Popen") as popen:
        module.main([])
    popen.assert_not_called()


def test_unreadable_hook_input_starts_nothing():
    module = load_module()
    with mock.patch.dict(os.environ, {"CMUX_SURFACE_ID": "surface-1"}, clear=True), \
            mock.patch.object(module.sys, "stdin", io.StringIO("{not json")), \
            mock.patch.object(module.subprocess, "Popen") as popen:
        module.main([])
    popen.assert_not_called()


def test_the_worker_job_carries_provider_and_normalized_payload():
    module = load_module()
    payload = {"sessionId": "sid", "cwd": "/r", "transcriptPath": "/t/x.jsonl",
               "agent": "droid"}
    with mock.patch.dict(os.environ, {"CMUX_SURFACE_ID": "surface-1"}, clear=True), \
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))), \
            mock.patch.object(module.subprocess, "Popen") as popen:
        module.main([])
    argv = popen.call_args.args[0]
    assert argv[:3] == [sys.executable, str(HOOK), "--worker"]
    assert json.loads(argv[3]) == {"provider": "factory", "payload": {
        **payload, "session_id": "sid", "cwd": "/r", "transcript_path": "/t/x.jsonl"}}
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["stdout"] is subprocess.DEVNULL


def test_a_broken_job_never_raises_out_of_the_hook(tmp_path):
    result = subprocess.run([sys.executable, str(HOOK), "--worker", "{not json"],
                            capture_output=True, text=True, timeout=30,
                            env={"HOME": str(tmp_path), "PATH": os.environ["PATH"],
                                 **COVERAGE_ENV})
    assert (result.returncode, result.stderr) == (0, "")
