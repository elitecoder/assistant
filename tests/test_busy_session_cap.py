"""The busy-session cap holds with the Observer off (bin/busy_ws.py + bin/pulse.py).

The pulse counts busy workspaces itself, from cmux and the transcripts, so the
cap of 5 binds whether or not the Observer builds contexts. These tests run the
real scan against a stand-in `cmux` executable and real transcript files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

import busy_ws
import cmux_sessions
from test_observer_skip import fixture_home, load_pulse

FAKE_CMUX = """#!{python}
import pathlib, sys
here = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
name = "tree" if "tree" in args else "sessions" if args[:2] == ["sessions", "list"] else None
if name is None:
    sys.exit(2)
sys.stdout.write((here / f"{{name}}.out").read_text())
sys.exit(int((here / f"{{name}}.rc").read_text()))
"""

TURN = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}
TOOL_CALL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]}}
TOOL_DONE = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}}


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class Fleet:
    """A stand-in cmux with workspaces and agent sessions, plus their transcripts."""

    def __init__(self, tmp_path, monkeypatch):
        self.home = fixture_home(tmp_path / "home")
        self.bin = tmp_path / "cmux-bin"
        self.bin.mkdir()
        cli = self.bin / "cmux"
        cli.write_text(FAKE_CMUX.format(python=sys.executable))
        cli.chmod(0o755)
        monkeypatch.setenv("CMUX_BIN", str(cli))
        monkeypatch.setenv("HOME", str(self.home))
        self.workspaces: list[dict] = []
        self.sessions: list[dict] = []
        self.tree_rc = 0
        self.sessions_rc = 0
        self.publish()

    def publish(self):
        tree = {"windows": [{"workspaces": self.workspaces}]}
        (self.bin / "tree.out").write_text(json.dumps(tree))
        (self.bin / "tree.rc").write_text(str(self.tree_rc))
        (self.bin / "sessions.out").write_text(json.dumps({"sessions": self.sessions}))
        (self.bin / "sessions.rc").write_text(str(self.sessions_rc))

    def workspace(self, n: int) -> str:
        ref = f"workspace:{n}"
        self.workspaces.append({"ref": ref, "id": f"WS-{n}", "panes": []})
        self.publish()
        return ref

    def agent(self, n: int, records, *, age=0, pid=None, agent="claude", transcript=True,
              active=True, path=None, heard=None):
        """An agent session in workspace n whose transcript holds `records` and was
        last written `age` seconds ago."""
        self.workspace(n)
        transcript_path = path or str(self.home / f".claude/projects/p/s{n}.jsonl")
        if transcript:
            file = Path(transcript_path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("".join(json.dumps(r) + "\n" for r in records))
            stamp = time.time() - age
            os.utime(file, (stamp, stamp))
        self.sessions.append({
            "session_id": f"s{n}", "agent": agent, "workspace_id": f"ws-{n}",
            "surface_id": f"sf-{n}", "pid": os.getpid() if pid is None else pid,
            "transcript_path": transcript_path if transcript else None,
            "active_for_surface": active,
            **({} if heard is None else {"updated_at_unix": time.time() - heard})})
        self.publish()
        return ref_of(n)


def ref_of(n: int) -> str:
    return f"workspace:{n}"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    return Fleet(tmp_path, monkeypatch)


# ─── the scan ────────────────────────────────────────────────────────────────

def test_a_turn_in_the_last_ten_minutes_is_busy_and_a_quiet_finished_one_is_not(fleet):
    fleet.agent(1, [TURN], age=60)
    fleet.agent(2, [TURN], age=1800)
    assert busy_ws.busy_refs() == {"workspace:1"}


def test_a_session_in_a_thirty_minute_tool_call_is_busy(fleet):
    fleet.agent(1, [TURN, TOOL_CALL], age=1800)
    fleet.agent(2, [TURN, TOOL_CALL, TOOL_DONE, TURN], age=1800)
    assert busy_ws.busy_refs() == {"workspace:1"}


def test_a_plain_terminal_and_an_exited_agent_are_not_busy(fleet):
    fleet.workspace(1)  # a terminal with no agent session
    fleet.agent(2, [TURN], age=5, pid=dead_pid())
    fleet.agent(3, [TURN], age=5, active=False)  # cleared off its surface
    fleet.agent(4, [TURN], age=5, agent="vim")  # not an agent the fleet runs
    fleet.agent(5, [TURN], age=5)
    fleet.sessions[-1]["pid"] = None  # a record that never learned its pid
    fleet.workspaces.append({"ref": "workspace:6"})  # a workspace the tree gives no id
    fleet.workspaces.append({"id": "WS-7"})  # nor a ref
    fleet.sessions.append({**fleet.sessions[0], "session_id": "gone", "workspace_id": "ws-99"})
    fleet.publish()  # a record for a workspace that is no longer open
    assert busy_ws.busy_refs() == set()


def test_an_unreadable_or_missing_transcript_counts_as_busy(fleet):
    fleet.agent(1, [], transcript=False)
    fleet.agent(2, [TURN], age=3600, path=str(fleet.home / "gone/s2.jsonl"))
    fleet.sessions[1]["transcript_path"] = str(fleet.home / "gone/nope.jsonl")
    fleet.publish()
    assert busy_ws.busy_refs() == {"workspace:1", "workspace:2"}


def test_a_transcript_the_scan_may_not_read_counts_as_busy(fleet):
    fleet.agent(1, [TURN], age=3600)
    path = Path(fleet.sessions[0]["transcript_path"])
    path.chmod(0)
    try:
        assert busy_ws.busy_refs() == {"workspace:1"}
    finally:
        path.chmod(0o600)


def test_an_agent_that_runs_under_another_process_name_still_counts(fleet, monkeypatch):
    """Copilot, or a Claude installed through npm, shows up as `node`. cmux's own records are
    trusted for what the process is; only registry rows are checked by name."""
    fleet.agent(1, [TURN], age=5, agent="copilot",
                path=str(fleet.home / ".copilot/session-state/s1/events.jsonl"))
    monkeypatch.setattr(cmux_sessions, "agent_process", lambda pid: False)
    assert busy_ws.busy_refs() == {"workspace:1"}


def test_a_stale_record_whose_pid_was_reused_cannot_hold_the_count(fleet):
    """cmux keeps its records across restarts and macOS reuses pids: the pid is alive, but the
    transcript is old, even with a tool call left open, so the session is not busy."""
    fleet.agent(1, [TURN, TOOL_CALL], age=3 * 3600)
    fleet.agent(2, [TURN], age=3600)
    assert busy_ws.busy_refs() == set()


def test_a_session_with_no_transcript_yet_is_judged_by_when_cmux_last_heard_from_it(fleet):
    """Opened and never prompted, or just cleared: it must not count for as long as it stays open."""
    fleet.agent(1, [], transcript=False, heard=30)
    fleet.agent(2, [], transcript=False, heard=2 * 3600)
    fleet.agent(3, [], transcript=False)  # no time on record: stay careful
    fleet.agent(4, [], heard=2 * 3600, path=str(fleet.home / ".claude/projects/p/gone.jsonl"))
    os.remove(fleet.home / ".claude/projects/p/gone.jsonl")
    assert busy_ws.busy_refs() == {"workspace:1", "workspace:3"}


def test_a_transcript_found_by_session_id_is_read_when_the_record_has_no_path(fleet):
    fleet.agent(1, [TURN], age=3600, path=str(fleet.home / ".claude/projects/p/s1.jsonl"))
    fleet.sessions[0]["transcript_path"] = None
    fleet.publish()
    assert busy_ws.busy_refs() == set()


def test_a_transcript_that_vanishes_mid_scan_counts_as_busy(fleet):
    fleet.agent(1, [TURN], age=3600)
    with mock.patch.object(busy_ws, "_transcript_signals", return_value=(None, "idle")):
        assert busy_ws.busy_refs() == {"workspace:1"}


def test_a_workspace_counts_once_however_many_sessions_it_holds(fleet):
    fleet.agent(1, [TURN], age=5)
    fleet.sessions.append({**fleet.sessions[0], "session_id": "second", "surface_id": "sf-b"})
    fleet.publish()
    assert busy_ws.busy_refs() == {"workspace:1"}


def test_copilot_and_droid_sessions_are_counted(fleet):
    fleet.agent(1, [{"type": "tool.execution_start", "data": {"toolCallId": "c1"}}],
                age=3000, agent="copilot",
                path=str(fleet.home / ".copilot/session-state/s1/events.jsonl"))
    fleet.agent(2, [TURN], age=30, agent="droid", path=str(fleet.home / ".factory/sessions/p/s2.jsonl"))
    fleet.agent(3, [TURN], age=3000, agent="droid", path=str(fleet.home / ".factory/sessions/p/s3.jsonl"))
    assert busy_ws.busy_refs() == {"workspace:1", "workspace:2"}


def test_a_session_only_the_registry_knows_is_counted_when_its_process_is_an_agent(fleet):
    fleet.workspace(1)
    transcript = fleet.home / ".claude/projects/p/reg.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(TURN) + "\n")
    registry = {"WS-1": {"session_id": "reg", "claude_pid": str(os.getpid()),
                         "transcript_path": str(transcript), "ts": 1.0}}
    (fleet.home / ".claude/cmux-registry.json").write_text(json.dumps(registry))
    with mock.patch.object(cmux_sessions, "agent_process", return_value=False):
        assert busy_ws.busy_refs() == set()
    with mock.patch.object(cmux_sessions, "agent_process", return_value=True):
        assert busy_ws.busy_refs() == {"workspace:1"}


@pytest.mark.parametrize("broken", ["tree", "sessions"])
def test_the_scan_says_so_when_cmux_cannot_list_workspaces_or_sessions(fleet, broken):
    fleet.agent(1, [TURN], age=5)
    setattr(fleet, f"{broken}_rc", 1)
    fleet.publish()
    with pytest.raises(busy_ws.Unavailable):
        busy_ws.busy_refs()


def test_a_tree_that_is_not_json_is_unavailable(fleet):
    (fleet.bin / "tree.out").write_text("not json")
    with pytest.raises(busy_ws.Unavailable):
        busy_ws.busy_refs()


def test_the_scan_makes_no_model_call(fleet):
    fleet.agent(1, [TURN], age=5)
    with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
        busy_ws.busy_refs()
    assert {c.args[0][1] for c in run.call_args_list} <= {"--id-format", "sessions"}


# ─── the pulse ───────────────────────────────────────────────────────────────

class Pulse:
    """main() with the Observer off (the default), a real scan, and N TODOs."""

    def __init__(self, fleet, todos=3):
        self.fleet = fleet
        self.mod = load_pulse(fleet.home)
        self.todos = [{"id": f"td-{i}", "priority": "P1"} for i in range(todos)]

    def run(self, *, batch=None, argv=(), dispatched=None):
        batch = batch if batch is not None else {
            "to_reclassify": [], "reuse_cached": [], "backed_off": [],
            "total_ws": len(self.fleet.workspaces)}
        with mock.patch.object(self.mod, "pick_ws_batch", return_value=batch), \
                mock.patch.object(self.mod, "purge_stale_awaiting"), \
                mock.patch.object(self.mod, "pick_open_todos", return_value={"bucket_b": list(self.todos)}), \
                mock.patch.object(self.mod, "dispatch_todo", return_value=True) as dispatch, \
                mock.patch.object(self.mod, "run", return_value=(0, "", "")) as run, \
                mock.patch.object(sys, "argv", ["pulse.py", *argv]):
            assert self.mod.main() == 0
        saved = [c for c in run.call_args_list if c.args and "state-write.py" in str(c.args[0])]
        payload = json.loads(saved[-1].kwargs["input_text"]) if saved else {}
        return dispatch, payload


def cards(payload):
    return {c["key"]: c for c in payload.get("awaiting_input", [])}


def test_five_recent_sessions_stop_dispatch_with_the_observer_off(fleet):
    for n in range(1, 6):
        fleet.agent(n, [TURN], age=120)
    dispatch, payload = Pulse(fleet).run()
    dispatch.assert_not_called()
    card = cards(payload)["dispatch-cap-hit"]
    assert card["title"] == "Dispatch capped — 3 TODO(s) waiting"
    assert card["detail"] == (
        "5 sessions are busy and the limit is 5. Sessions you are using count: a session is busy when it had "
        "a turn in the last 10 minutes or is running a tool. Busy now: workspace:1, workspace:2, workspace:3, "
        "workspace:4, workspace:5. Holding: td-0, td-1, td-2. They start when there is room.")


def test_four_busy_sessions_leave_room_for_the_per_pulse_limit(fleet):
    for n in range(1, 5):
        fleet.agent(n, [TURN], age=120)
    fleet.agent(5, [TURN], age=3600)
    dispatch, payload = Pulse(fleet).run()
    assert [c.args[0] for c in dispatch.call_args_list] == ["td-0", "td-1"]
    assert "dispatch-cap-hit" not in cards(payload)


def test_sessions_in_long_tool_calls_fill_the_cap(fleet):
    for n in range(1, 6):
        fleet.agent(n, [TURN, TOOL_CALL], age=1800)
    dispatch, payload = Pulse(fleet).run()
    dispatch.assert_not_called()
    assert "5 sessions are busy and the limit is 5." in cards(payload)["dispatch-cap-hit"]["detail"]


def test_a_tool_call_left_open_for_hours_is_a_session_waiting_on_you_not_a_busy_one(fleet):
    """Two sessions like these, each open for more than a day, sat on the maintainer's machine."""
    fleet.agent(1, [TURN, TOOL_CALL], age=2 * 3600 - 60)
    fleet.agent(2, [TURN, TOOL_CALL], age=2 * 3600 + 60)
    fleet.agent(3, [TURN, TOOL_CALL], age=30 * 3600)
    assert busy_ws.busy_refs() == {"workspace:1"}


def test_stale_open_tool_calls_do_not_hold_dispatch(fleet):
    for n in range(1, 6):
        fleet.agent(n, [TURN, TOOL_CALL], age=26 * 3600)
    dispatch, payload = Pulse(fleet).run()
    assert dispatch.call_count == 2 and "dispatch-cap-hit" not in cards(payload)


@pytest.mark.parametrize("status, age, active", [
    ("working", 60, True), ("working", 2 * 3600 - 1, True), ("working", 2 * 3600, False),
    ("working", None, True),  # can't tell how long it has been open, so be careful
    ("idle", 599, True), ("idle", 600, False), ("idle", None, False), (None, 99999, False),
])
def test_the_shared_rule_for_a_busy_session(status, age, active):
    assert busy_ws.config.ws_is_active(status, age) is active


def test_the_total_limit_is_named_when_it_is_the_one_holding(fleet):
    fleet.agent(1, [TURN], age=3600)
    dispatch, payload = Pulse(fleet).run(batch={
        "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 30})
    dispatch.assert_not_called()
    assert cards(payload)["dispatch-cap-hit"]["detail"] == (
        "30 workspaces are open and the limit is 30. Close the ones you no longer need. "
        "Holding: td-0, td-1, td-2. They start when there is room.")


def test_the_busy_reason_names_no_sessions_when_only_the_observer_saw_them(fleet):
    assert load_pulse(fleet.home).cap_reason(5, 12, None) == (
        "5 sessions are busy and the limit is 5. Sessions you are using count: a session is busy when it had "
        "a turn in the last 10 minutes or is running a tool.")


def test_busy_sessions_are_named_in_workspace_order(fleet):
    assert "Busy now: workspace:2, workspace:9, workspace:10." in load_pulse(fleet.home).cap_reason(
        5, 12, {"workspace:10", "workspace:9", "workspace:2"})


def test_dispatch_is_held_when_the_workspace_list_could_not_be_read_then_resumes(fleet):
    fleet.agent(1, [TURN], age=3600)
    pulse = Pulse(fleet)
    dispatch, payload = pulse.run(batch={
        "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0,
        "unavailable": True})
    dispatch.assert_not_called()
    card = cards(payload)["dispatch-held-no-fleet-view"]
    assert card["title"] == "Dispatch paused — 3 TODO(s) waiting"
    assert card["detail"] == (
        "Couldn't count busy sessions: the list of your open workspaces could not be read. Holding: td-0, td-1, "
        "td-2. Nothing is lost. Assistant looks again every few minutes and starts them once it can count.")
    assert "dispatch-cap-hit" not in cards(payload)
    dispatch, payload = pulse.run()
    assert [c.args[0] for c in dispatch.call_args_list] == ["td-0", "td-1"]
    assert "dispatch-held-no-fleet-view" not in cards(payload)


def test_dispatch_is_held_when_the_busy_sessions_could_not_be_counted(fleet):
    fleet.agent(1, [TURN], age=3600)
    fleet.sessions_rc = 1
    fleet.publish()
    pulse = Pulse(fleet, todos=7)
    dispatch, payload = pulse.run()
    dispatch.assert_not_called()
    card = cards(payload)["dispatch-held-no-fleet-view"]
    assert "cmux did not answer when asked for your sessions" in card["detail"]
    assert "td-4 (+2 more)" in card["detail"]
    fleet.sessions_rc = 0
    fleet.publish()
    dispatch, payload = pulse.run()
    assert dispatch.call_count == 2
    assert "dispatch-held-no-fleet-view" not in cards(payload)


def test_the_scan_never_raises_into_the_pulse(fleet):
    mod = load_pulse(fleet.home)
    with mock.patch.object(busy_ws, "busy_refs", side_effect=ValueError("boom")):
        assert mod.scan_busy_ws() is None
    fleet.agent(1, [TURN], age=5)
    assert mod.scan_busy_ws() == {"workspace:1"}


def test_a_dry_run_dispatches_nothing_and_scans_nothing(fleet):
    for n in range(1, 4):
        fleet.agent(n, [TURN], age=5)
    pulse = Pulse(fleet)
    with mock.patch.object(pulse.mod, "scan_busy_ws") as scan:
        dispatch, _ = pulse.run(argv=["--dry-run"])
    dispatch.assert_not_called()
    scan.assert_not_called()


def test_nothing_is_scanned_when_no_task_is_waiting(fleet):
    fleet.agent(1, [TURN], age=5)
    pulse = Pulse(fleet, todos=0)
    with mock.patch.object(pulse.mod, "scan_busy_ws") as scan:
        dispatch, payload = pulse.run()
    scan.assert_not_called()
    dispatch.assert_not_called()
    assert not cards(payload)


# ─── count_active: the union with the Observer's contexts ────────────────────

def test_the_count_is_the_union_of_the_observers_view_and_the_scan(fleet):
    mod = load_pulse(fleet.home)
    meta = [
        {"ref": "workspace:1", "ctx": {"agent_status": "working", "last_turn_age_sec": 1800}},
        {"ref": "workspace:2", "ctx": {"agent_status": "idle", "last_turn_age_sec": 9999}},
        {"ref": "workspace:3", "ctx": None},
        {"ctx": {"agent_status": "idle", "last_turn_age_sec": 5}},
        {"ctx": {"agent_status": "idle", "last_turn_age_sec": 5}},
    ]
    assert mod.count_active(meta) == 3  # the Observer-on count, unchanged
    # Workspace 1 is in both lists and counts once; 3 and 7 only the scan sees.
    assert mod.count_active(meta, {"workspace:1", "workspace:3", "workspace:7"}) == 5
    assert mod.count_active([], {"workspace:1"}) == 1


def test_with_the_observer_on_the_count_never_drops_below_what_its_contexts_say(fleet):
    for n in range(1, 4):
        fleet.agent(n, [TURN], age=3600)
    (fleet.home / ".assistant/config.json").write_text(json.dumps({"observer": {"enabled": True}}))
    pulse = Pulse(fleet)
    ctx = {"ws_ref": "workspace:1", "agent_status": "working", "last_turn_age_sec": 5,
           "transcript_path": None, "is_protected": False, "title": "t", "cwd": "/",
           "screen_text": "s", "screen_shows_error": False}
    batch = {"to_reclassify": [{"ref": f"workspace:{n}", "title": "t", "cwd": "/"} for n in (1, 2, 3)],
             "reuse_cached": [], "backed_off": [], "total_ws": 3}
    seen = {}
    real = pulse.mod.count_active

    def spy(meta, busy=()):
        seen["n"] = real(meta, busy)
        return seen["n"]

    with mock.patch.object(pulse.mod, "build_ctx", return_value=ctx), \
            mock.patch.object(pulse.mod, "call_observer_batch", return_value=({}, {})), \
            mock.patch.object(pulse.mod, "save_summary"), \
            mock.patch.object(pulse.mod, "cmux_send"), \
            mock.patch.object(pulse.mod, "count_active", side_effect=spy):
        pulse.run(batch=batch)
    # All three contexts say working; the scan sees all three idle: union is 3.
    assert seen["n"] == 3
