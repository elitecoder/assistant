"""Assistant's own scripts, as they are, running a fleet kept in tmux.

Pro mode with {"sessions": {"backend": "tmux"}} in config.json, a real tmux server per test, and a stand-in
for the agent (tests/tmux_fakes.py). Nothing here stubs the session tool: the pulse, the send script, the
world scanner, the busy count, the comms daemon's checks, the Observer's context builder, the needs-input
watcher and the Work queue's "open workspace" all run the commands they always run, and tmux answers.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pty
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import agent_session
import busy_ws
import comms_lib
import comms_session
import tmux_backend as tb
from tmux_fakes import BACKEND, TMUX, FakeClaude, needs_tmux, server, tmux, until  # noqa: F401 - `server` is a fixture
from work_queue import server as wq_server
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401 - `wq` is a fixture

REPO = Path(__file__).resolve().parent.parent
pytestmark = needs_tmux

# What the stand-in agent prints when it starts: Claude Code's banner, which the pulse waits for.
AGENT = r'''
import json, pathlib, sys
transcript = pathlib.Path(sys.argv[1])
print("Claude Code v9.9.9")
print("  ⏵⏵ bypass permissions on (shift+tab to cycle)", flush=True)
for line in sys.stdin:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with open(transcript, "a") as out:
        out.write(json.dumps({"type": "user", "sessionId": transcript.stem,
                              "message": {"role": "user", "content": line.strip()}}) + "\n")
    print("agent read:", line.strip(), flush=True)
'''


@pytest.fixture
def home(server, monkeypatch):
    """A home whose config keeps sessions in tmux, on the test's own tmux server."""
    monkeypatch.setenv("HOME", str(server))
    monkeypatch.delenv("CMUX_BIN")
    (server / ".assistant").mkdir()
    (server / ".assistant" / "config.json").write_text(
        json.dumps({"mode": "pro", "sessions": {"backend": "tmux"}}))
    (server / ".claude" / "cache").mkdir(parents=True)
    (server / "work").mkdir()
    return server


@pytest.fixture
def claude(home, monkeypatch):
    return FakeClaude(home, monkeypatch)


def backend(argv: list) -> str:
    done = subprocess.run([sys.executable, str(BACKEND), *argv], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here while loading
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


def new(home, title: str) -> str:
    return backend(["new-workspace", "--cwd", str(home / "work"), "--name", title]).split()[1]


def screen(ws_ref: str) -> str:
    return backend(["read-screen", "--workspace", ws_ref, "--lines", "60"])


def script(home, name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "bin" / name), *args], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(home)}, timeout=60)


# ── starting a task ───────────────────────────────────────────────────────────

def test_the_pulse_starts_a_task_in_tmux_and_its_agent_receives_the_instruction(home, monkeypatch):
    pulse = load("pulse.py", "pulse_on_tmux")
    assert pulse.CMUX_BIN == str(BACKEND)
    pulse.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    pulse.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    pulse.DISPATCH_CWD = home / "work"
    pulse.TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    pulse.TODO_PATH.write_text(json.dumps({"items": [
        {"id": "td-1", "title": "Fix the flaky test", "status": "open", "autoDispatch": True, "source": "todo"}]}))
    transcript = agent_session.confirm_dir(agent_session.CLAUDE, home / "work", home=home) / "sess-dispatch.jsonl"
    stand_in = home / "agent.py"
    stand_in.write_text(AGENT)
    monkeypatch.setattr(agent_session, "launch_command",
                        lambda agent, home=None, **k: f"{sys.executable} {stand_in} {transcript}")
    monkeypatch.setattr(agent_session, "available_or_claude", lambda agent: agent)

    assert pulse.dispatch_todo("td-1") is True

    (ws,) = [w for window in tb.tree()["windows"] for w in window["workspaces"]]
    assert ws["title"] == "td-1: Fix the flaky test" and ws["ref"] == "workspace:1"
    task = json.loads(pulse.TODO_PATH.read_text())["items"][0]
    assert (task["status"], task["dispatchedWs"]) == ("in-progress", "workspace:1")
    (prompt,) = (home / "spawn-prompts").glob("prompt-dispatch-td-1-*.md")
    instruction = f"Read {prompt} in full and execute every instruction in it."
    assert f"agent read: {instruction}" in screen("workspace:1").replace("\n", "")
    typed = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [record["message"]["content"] for record in typed] == [instruction]
    # Pro mode's record of what it typed names the tmux workspace, and says the agent received it.
    recorded = [json.loads(line) for path in sorted((home / ".assistant" / "effects").rglob("*.jsonl"))
                for line in path.read_text().splitlines()]
    assert any(row.get("target", {}).get("session") == "workspace:1" for row in recorded)
    assert any(row.get("outcome") == "done" for row in recorded)
    # A workspace left in tmux is counted, so the same task is not started twice.
    assert tmux("list-sessions", "-F", "#{session_name}").split() == ["td-1-Fix-the-flaky-test"]


def test_a_task_is_not_started_when_tmux_is_not_installed(home, monkeypatch):
    monkeypatch.setenv("ASSISTANT_TMUX_BIN", str(home / "no-tmux-here"))
    pulse = load("pulse.py", "pulse_without_tmux")
    pulse.TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    pulse.TODO_PATH.write_text(json.dumps({"items": [
        {"id": "td-1", "title": "Fix it", "status": "open", "autoDispatch": True, "source": "todo"}]}))
    assert pulse.dispatch_todo("td-1") is False
    assert json.loads(pulse.TODO_PATH.read_text())["items"][0]["status"] == "open"


# ── nudging ───────────────────────────────────────────────────────────────────

def test_the_send_script_types_a_nudge_into_a_tmux_session_and_logs_it(home):
    ref = new(home, "td-2: Stalled work")
    sent = script(home, "cmux-send.py", "--ws", ref, "--text", "echo nudged-$((20+2))", "--enter",
                  "--post-send-wait", "0.2")
    assert sent.returncode == 0, sent.stdout + sent.stderr
    record = json.loads(sent.stdout)
    surface = tb.tree()["windows"][0]["workspaces"][0]["panes"][0]["surfaces"][0]
    assert (record["outcome"], record["target_ws_title"]) == ("sent", "td-2: Stalled work")
    assert (record["target_surface_id"], record["target_surface_ref"], record["target_tty"]) == (
        surface["id"], "surface:1", surface["tty"])
    until(lambda: "nudged-22" in screen(ref))
    (logged,) = [json.loads(line) for line in (home / ".assistant" / "sends.jsonl").read_text().splitlines()]
    assert logged["text"] == "echo nudged-$((20+2))" and logged["target_ws_ref"] == ref

    gone = script(home, "cmux-send.py", "--ws", "workspace:9", "--text", "hello", "--post-send-wait", "0")
    assert (gone.returncode, json.loads(gone.stdout)["outcome"]) == (1, "no_terminal_surface")


def test_the_dashboards_peek_reads_a_tmux_workspaces_screen(home):
    ref = new(home, "td-3: Peek at me")
    backend(["send", "--workspace", ref, "echo peeked-$((30+3))"])
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: "peeked-33" in screen(ref))
    peeked = json.loads(script(home, "tools/workspace-peek.py", "--ws", ref, "--lines", "20").stdout)
    assert peeked["surface_ref"] == "surface:1" and "peeked-33" in peeked["screen_text"]
    assert "error" in json.loads(script(home, "tools/workspace-peek.py", "--ws", "workspace:9").stdout)


def test_the_dashboard_and_the_receipt_writer_read_tmux_workspace_titles(home):
    ref = new(home, "td-3: A title to show")
    page = load("render-assistant-page.py", "render_page_on_tmux")
    assert page._cmux_workspaces()[ref]["title"] == "td-3: A title to show"
    assert load("tools/write-receipt.py", "write_receipt_on_tmux").cmux_title(ref) == "td-3: A title to show"
    assert load("tools/memory_seeds.py", "memory_seeds_on_tmux")._cmux_workspaces() == {ref: "td-3: A title to show"}


# ── monitoring ────────────────────────────────────────────────────────────────

def test_the_world_scanner_lists_tmux_workspaces_and_verifies_the_claude_session_in_one(home, claude):
    plain, with_agent = new(home, "notes"), new(home, "td-4: Agent at work")
    pid = claude.agent(lambda argv: backend(argv), with_agent)
    claude.rows([claude.row(pid, "aaaaaaaa-1111-2222-3333-444444444444", status="busy")])
    scanned = script(home, "world-scanner.py")
    assert scanned.returncode == 0, scanned.stderr
    world = json.loads((home / ".claude" / "cache" / "world.json").read_text())
    by_ref = {ws["ws_ref"]: ws for ws in world["workspaces"]}
    assert (by_ref[plain]["title"], by_ref[with_agent]["title"]) == ("notes", "td-4: Agent at work")
    tree = {ws["ref"]: ws for window in tb.tree()["windows"] for ws in window["workspaces"]}
    assert by_ref[with_agent]["workspace_id"] == tree[with_agent]["id"]
    (session,) = world["live_sessions"]
    assert (session["session_id"], session["provider"], session["pid"]) == (
        "aaaaaaaa-1111-2222-3333-444444444444", "claude", pid)
    assert (session["ws_ref"], session["identity_status"]) == (with_agent, "verified")
    assert session["surface_id"] == tree[with_agent]["panes"][0]["surfaces"][0]["id"]
    assert by_ref[plain]["surfaces"][0]["session_id"] is None


def test_the_busy_count_sees_a_claude_session_at_work_in_tmux(home, claude):
    idle, working = new(home, "idle"), new(home, "td-5: Working")
    pid = claude.agent(lambda argv: backend(argv), working)
    claude.rows([claude.row(pid, "bbbbbbbb-1111-2222-3333-444444444444")])  # opened a moment ago, no transcript yet
    assert busy_ws.busy_refs() == {working}
    claude.rows([claude.row(pid, "bbbbbbbb-1111-2222-3333-444444444444",
                            startedAt=int((time.time() - 7200) * 1000))])
    assert busy_ws.busy_refs() == set()
    assert idle not in busy_ws.busy_refs()


def test_the_observers_context_attaches_the_transcript_of_the_claude_session_in_the_pane(home, claude):
    ref = new(home, "td-6: Observed")
    sid = "cccccccc-1111-2222-3333-444444444444"
    transcript = agent_session.confirm_dir(agent_session.CLAUDE, home / "work", home=home) / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "sessionId": sid,
                                      "message": {"role": "user", "content": "hello"}}) + "\n")
    backend(["send", "--workspace", ref, "echo 'Claude Code v9.9.9'"])
    backend(["send-key", "--workspace", ref, "enter"])
    until(lambda: "Claude Code v9.9.9\n" in screen(ref))
    pid = claude.agent(lambda argv: backend(argv), ref)
    claude.rows([claude.row(pid, sid)])
    context = load("build-ws-context.py", "build_ws_context_on_tmux")
    before = context.workspace_identity_snapshot(ref)
    resolved = context.resolve_workspace_screen_and_transcript(ref)
    assert resolved["transcript_path"] == str(transcript)
    assert "Claude Code v9.9.9" in resolved["screen_text"]
    identity = context.observation_identity(ref, before, context.workspace_identity_snapshot(ref), resolved,
                                            time.time())
    surface = tb.tree()["windows"][0]["workspaces"][0]["panes"][0]["surfaces"][0]
    assert identity["observed_sessions"] == [{"surface_id": surface["id"], "provider": "claude", "session_id": sid}]


def test_the_needs_input_watcher_hears_a_tmux_session_start_waiting(home, claude):
    ref = new(home, "td-7: Needs you")
    pid = claude.agent(lambda argv: backend(argv), ref)
    sid = "dddddddd-1111-2222-3333-444444444444"
    claude.rows([claude.row(pid, sid)])
    watcher = load("cmux-watcher.py", "cmux_watcher_on_tmux")
    ws = tb.tree()["windows"][0]["workspaces"][0]
    resolver = watcher.WsRefResolver()
    assert (resolver.resolve(ws["id"]), resolver.title(ws["id"])) == (ref, "td-7: Needs you")
    assert watcher.cmux_available() is True
    assert "claude" in watcher.read_screen(ws["id"])  # the stand-in agent's command line is on the screen

    # The watcher runs the backend's `events`, which looks every couple of seconds. The session goes back and
    # forth between idle and waiting for longer than that, so a look catches it starting to wait.
    stop = threading.Event()

    def come_and_go():
        waiting = False
        while not stop.wait(tb.EVENTS_POLL_SEC + 1):
            waiting = not waiting
            claude.rows([claude.row(pid, sid, **({"status": "waiting", "waitingFor": "input needed"}
                                                 if waiting else {}))])
    threading.Thread(target=come_and_go, daemon=True).start()
    heard = []
    stream = watcher.stream_events(stop_flag=stop.is_set)
    listener = threading.Thread(target=lambda: heard.append(next(stream)), daemon=True)
    listener.start()
    listener.join(timeout=60)
    stop.set()
    assert heard, "no event in a minute"
    stream.close()
    assert watcher.classify_event(heard[0]) == {
        "signal": "needs_input", "request_id": heard[0]["payload"]["_opencode_request_id"],
        "workspace_id": ws["id"], "cwd": str(home), "session_id": sid, "event_name": "agent.hook.Notification"}


# ── the comms daemon's checks, and opening a workspace ─────────────────────────

def test_the_comms_daemon_tells_a_live_tmux_workspace_from_one_that_is_gone(home):
    paths = comms_lib.Paths.from_env()
    assert str(paths.cmux_bin) == str(BACKEND)
    warm = new(home, comms_session.SESSION_TITLE)
    work = new(home, "td-8: User work")
    assert comms_session.probe_workspace(paths, warm) == comms_session.ALIVE
    assert comms_session.probe_workspace(paths, "workspace:9") == comms_session.GONE
    listed = comms_session.list_workspaces_all_windows(paths)
    assert comms_session.ref_listed(listed, warm) and comms_session.ref_listed(listed, work)
    assert not comms_lib.cmux_read_screen(paths, work).startswith("(cmux read-screen failed")
    assert comms_lib.cmux_read_screen(paths, "workspace:9").startswith("(cmux read-screen failed rc=1")
    assert comms_lib.sidebar_numbers(paths) == {}  # tmux has no numbered sidebar
    notes = []
    comms_session.close_own_workspace(paths, work, log=notes.append)  # not its own: left alone
    assert notes == [f"skip close {work}: not a '{comms_session.SESSION_TITLE}' workspace (ref reissued?)"]
    comms_session.close_own_workspace(paths, warm, log=notes.append)
    assert notes[-1] == f"closed prior warm workspace {warm}"
    assert comms_session.probe_workspace(paths, warm) == comms_session.GONE
    assert comms_session.probe_workspace(paths, work) == comms_session.ALIVE


def test_open_workspace_selects_the_tasks_tmux_window_and_never_raises_the_cmux_app(wq, server, monkeypatch):
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant" / "config.json").write_text(json.dumps({"mode": "pro", "sessions": {"backend": "tmux"}}))
    monkeypatch.delenv("CMUX_BIN", raising=False)
    monkeypatch.setenv("ASSISTANT_TMUX_BIN", TMUX)  # the fixture's PATH holds only its fake tools
    fake_tool(wq.bin, "open", [("-b com.cmuxterm.app", "")])
    (wq.home / "work").mkdir()
    new(wq.home, "elsewhere")
    ref = new(wq.home, "td-060: Fix the ruler")
    wq.todo.write_text(json.dumps({"items": [{"id": "td-060", "dispatchedWs": ref}]}))
    refused = wq_server.focus_workspace({"id": "td-060"})
    assert refused[0] == 502 and "no terminal is attached to tmux" in refused[1]["error"]
    controller, terminal = pty.openpty()
    client = subprocess.Popen([TMUX, "attach", "-t", "elsewhere"], stdin=terminal, stdout=terminal,
                              stderr=terminal, env={**os.environ, "TERM": "xterm"})
    try:
        until(lambda: tmux("list-clients", "-F", "#{client_session}").strip() == "elsewhere")
        assert wq_server.focus_workspace({"id": "td-060"}) == (200, {"workspace": ref, "window": "window:2"})
        assert tmux("list-clients", "-F", "#{client_session}").strip() == "td-060-Fix-the-ruler"
    finally:
        client.terminate()
        client.wait(timeout=10)
        os.close(controller)
        os.close(terminal)
    assert calls(wq.bin, "open") == []


def test_the_doctor_checks_tmux_in_cmuxs_place(home, monkeypatch):
    doctor = load("assistant-doctor.py", "assistant_doctor_on_tmux")
    found = doctor.check_cmux()
    assert (found.name, found.status, found.core) == ("tmux", "PASS", True)
    assert found.detail == "sessions are kept in tmux (ping ok)"
    monkeypatch.setenv("ASSISTANT_TMUX_BIN", str(home / "no-tmux-here"))
    missing = doctor.check_cmux()
    assert (missing.name, missing.status, missing.core) == ("tmux", "FAIL", True)
    assert missing.detail.startswith("sessions are kept in tmux, and it doesn't run: Error: unavailable: tmux can't")
    assert missing.remedy.startswith("install tmux (brew install tmux")
