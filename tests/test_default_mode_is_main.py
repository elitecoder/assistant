"""In the default mode every path the session-typing change touches does what origin/main does.

Each test runs the fakes a path always had and asserts what origin/main does for that input, as literals: the
keystrokes in order, the task file, `sends.jsonl`, what a script prints and its exit status, what a route replies, the
Slack sends, the cards, and that `~/.assistant/effects` is not made. The file uses only what both trees have, so it
also passes when run inside a checkout of origin/main, which is how the literals were found and how to check them again:

    mkdir /tmp/main-copy && git archive origin/main | tar -x -C /tmp/main-copy
    cp tests/test_default_mode_is_main.py /tmp/main-copy/tests/ && cd /tmp/main-copy && python -m pytest tests/test_default_mode_is_main.py

Every test in it passes there. Anything that needs code only this branch has is in `tests/test_session_gate.py`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from assistant import mode
from test_todo_server import env  # noqa: F401 - the run-now tests' fixture

REPO = Path(__file__).resolve().parent.parent
READY = "Claude Code v1.2.3  ⏵⏵ bypass permissions on"
TRUST = "1. Yes, I trust this folder"
WS = "workspace:1000010007"
MARKER = "TEXT-MARKER"


@pytest.fixture(autouse=True)
def default_mode(request, monkeypatch, tmp_path):
    """The default mode, said outright (a test marked `real_mode` reads the config file instead), and a home of its
    own for the test."""
    if "real_mode" not in request.keywords:
        monkeypatch.setattr(mode, "current", lambda: "default")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home/.assistant").mkdir(parents=True)
    (tmp_path / "home/.claude/cache").mkdir(parents=True)
    return tmp_path / "home"


def effects_untouched(home: Path) -> None:
    assert not (home / ".assistant/effects").exists()


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- a task started by the pulse --------------------------------------------------------------------------------

class Dispatch:
    """cmux, faked: records every keystroke call the pulse makes and answers the rest."""

    def __init__(self, home, screens=(READY,), workspace=WS, refuse_text=False, timeout_text=False, **item):
        self.home, self.calls, self.refuse_text, self.workspace = home, [], refuse_text, workspace
        self.timeout_text = timeout_text
        self.screens = iter(screens)
        self.mod = load("pulse_default_mode", "pulse.py")
        self.mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
        self.mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
        self.mod.DISPATCH_CWD = home / "dev"
        (home / "dev").mkdir(exist_ok=True)
        self.mod.TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.TODO_PATH.write_text(json.dumps({"items": [
            {"id": "td-1", "title": "Fix it", "status": "open", "autoDispatch": True, **item}]}))

    def run(self, cmd, **k):
        if cmd[1] == "new-workspace":
            return (0, self.workspace, "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    def rpc(self, method, params, timeout=15):
        self.calls.append((method, params))
        if self.timeout_text and method == "surface.send_text":
            self.mod._last_rpc_rc = 124  # what the real `_cmux_rpc` leaves behind when cmux times out (a no-op on main)
            return None
        return None if self.refuse_text and method == "surface.send_text" else {}

    def go(self):
        with mock.patch.object(self.mod, "run", side_effect=self.run), \
                mock.patch.object(self.mod, "_cmux_rpc", side_effect=self.rpc), \
                mock.patch.object(self.mod, "_surface_read_text",
                                  side_effect=lambda r, lines=200: next(self.screens, READY)), \
                mock.patch.object(self.mod.time, "sleep", lambda *a, **k: None):
            return self.mod.dispatch_todo("td-1")

    def task(self) -> dict:
        return json.loads(self.mod.TODO_PATH.read_text())["items"][0]


def instruction(call) -> bool:
    text = call[1].get("text", "")
    return call[0] == "surface.send_text" and text.startswith("Read ") \
        and text.endswith(" in full and execute every instruction in it.")


ENTER = ("surface.send_key", {"surface_id": "surface:3", "key": "enter"})


def test_a_task_is_started_with_two_keystroke_calls_and_nothing_is_recorded(default_mode):
    run = Dispatch(default_mode)
    assert run.go() is True
    assert len(run.calls) == 2 and instruction(run.calls[0]) and run.calls[1] == ENTER
    assert run.calls[0][1]["surface_id"] == "surface:3"
    task = run.task()
    assert (task["status"], task["dispatchedWs"], task["statusReason"]) == ("in-progress", WS, f"dispatched to {WS}")
    assert "parkedBy" not in task
    effects_untouched(default_mode)


def test_a_task_that_meets_the_trust_prompt_answers_it_first(default_mode):
    run = Dispatch(default_mode, screens=(TRUST, READY))
    assert run.go() is True
    assert run.calls[:2] == [("surface.send_text", {"surface_id": "surface:3", "text": "1"}), ENTER]
    assert instruction(run.calls[2]) and run.calls[3] == ENTER and len(run.calls) == 4
    effects_untouched(default_mode)


def test_when_cmux_refuses_the_text_the_enter_is_still_pressed_and_the_task_is_still_stamped(default_mode):
    run = Dispatch(default_mode, refuse_text=True)
    assert run.go() is True
    assert [c[0] for c in run.calls] == ["surface.send_text", "surface.send_key"]
    assert run.task()["status"] == "in-progress" and "parkedBy" not in run.task()
    effects_untouched(default_mode)


def test_a_time_out_typing_the_instruction_is_ignored_as_before_and_the_enter_still_follows(default_mode):
    run = Dispatch(default_mode, timeout_text=True)
    assert run.go() is True and [c[0] for c in run.calls] == ["surface.send_text", "surface.send_key"]
    assert run.task()["status"] == "in-progress"
    effects_untouched(default_mode)


def test_a_slack_capture_that_isnt_auto_start_is_still_started_by_the_function(default_mode):
    run = Dispatch(default_mode, source="slack-react:C1:1.2", autoDispatch=None)
    assert run.go() is True and len(run.calls) == 2
    effects_untouched(default_mode)


def test_a_session_number_of_any_length_is_typed_into(default_mode):
    run = Dispatch(default_mode, workspace="workspace:" + "9" * 19)
    assert run.go() is True and len(run.calls) == 2
    assert run.task()["dispatchedWs"] == "workspace:" + "9" * 19
    effects_untouched(default_mode)


def test_an_unwritable_effects_folder_changes_nothing_when_a_task_is_started(default_mode):
    (default_mode / ".assistant/effects").write_text("a file where the folder would go")
    run = Dispatch(default_mode)
    assert run.go() is True and len(run.calls) == 2 and run.task()["status"] == "in-progress"
    assert (default_mode / ".assistant/effects").read_text() == "a file where the folder would go"
    assert list((default_mode / ".assistant").glob("**/.probe*")) == []


def test_a_good_dispatch_leaves_a_stale_park_marker_as_it_found_it(default_mode):
    run = Dispatch(default_mode, parkedBy="typing_refused")
    assert run.go() is True and run.task().get("parkedBy") == "typing_refused"


def test_no_card_is_added_for_a_parked_task(fleet_default):
    pulse, payload = fleet_default
    assert [c["key"] for c in payload.get("awaiting_input", [])] == []


@pytest.fixture
def fleet_default(default_mode):
    """The pulse's own main(), with a parked task in the task file and nothing else to say."""
    return _run_pulse_main(default_mode)


def _run_pulse_main(home):
    pulse = load("pulse_default_main", "pulse.py")
    pulse.TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    pulse.TODO_PATH.write_text(json.dumps({"items": [
        {"id": "td-9", "title": "Parked", "status": "blocked", "parkedBy": "typing_refused", "dispatchedWs": WS}]}))
    batch = {"to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0}
    with mock.patch.object(pulse, "pick_ws_batch", return_value=batch), mock.patch.object(pulse, "purge_stale_awaiting"), \
            mock.patch.object(pulse, "pick_open_todos", return_value={"bucket_b": []}), \
            mock.patch.object(pulse, "run", return_value=(0, "", "")) as run, \
            mock.patch.object(pulse, "self_update_pulse", lambda pulse_idx: None), \
            mock.patch.object(sys, "argv", ["pulse.py"]):
        assert pulse.main() == 0
    saved = [c for c in run.call_args_list if c.args and "state-write.py" in str(c.args[0])]
    return pulse, json.loads(saved[-1].kwargs["input_text"])


# --- a nudge, /cleanup and a merge request from the pulse --------------------------------------------------------

PULSE_SEND = [sys.executable, str(REPO / "bin/cmux-send.py"), "--ws", WS, "--text", "/cleanup", "--enter",
              "--caller", "assistant-pulse"]


def test_the_pulse_runs_the_send_script_with_only_the_arguments_it_always_had(default_mode):
    pulse = load("pulse_default_send", "pulse.py")
    with mock.patch.object(pulse, "run", return_value=(0, json.dumps({"outcome": "sent"}), "")) as ran:
        assert pulse.cmux_send(WS, "/cleanup") == {"outcome": "sent"}
    assert ran.call_args.args == (PULSE_SEND,) and ran.call_args.kwargs == {}


@pytest.mark.parametrize("rc", [2, 4, 5])
def test_every_failed_exit_status_of_the_send_script_is_one_failed_send_to_the_pulse(default_mode, rc):
    pulse = load("pulse_default_send2", "pulse.py")
    with mock.patch.object(pulse, "run", return_value=(rc, "{}", "boom")):
        assert pulse.cmux_send(WS, "/cleanup") == {"outcome": "failed", "transcript_size_delta": 0}


def test_a_verdict_whose_send_failed_is_recorded_as_before(default_mode):
    pulse = load("pulse_default_verdict", "pulse.py")
    ws = {"ref": WS, "title": "t", "cwd": "/tmp"}
    with mock.patch.object(pulse, "cmux_send", return_value={"outcome": "failed", "transcript_size_delta": 0}), \
            mock.patch.object(pulse, "previous_send_ingested", return_value=True), \
            mock.patch.object(pulse, "observer_actions_held", return_value=False), \
            mock.patch.object(pulse, "pre_cleanup_check", return_value={"gate": "ok"}), \
            mock.patch.object(pulse, "assistant_merged_workspace", return_value=True):
        action = pulse.execute_verdict(ws, {"verdict": "ready_for_cleanup", "summary": "s", "next": "n"}, [])
    assert (action["kind"], action["outcome"], action["evidence"]) == (
        "ready_for_cleanup", "failed", "sent '/cleanup' delta=0")


def test_the_pulse_runs_the_merge_script_with_only_its_old_arguments(default_mode):
    pulse = load("pulse_default_merge", "pulse.py")
    with mock.patch.object(pulse.subprocess, "run",
                           return_value=mock.Mock(returncode=0, stdout=json.dumps({"outcome": "submitted"}))) as ran:
        pulse.run_merge_pr_dispatch(WS, 7, True, False)
    assert ran.call_args.args == ([sys.executable, str(REPO / "bin/merge-pr-dispatch.py"), "--ws", WS, "--pr", "7",
                                   "--refactor-attested"],)
    assert ran.call_args.kwargs == {"capture_output": True, "text": True, "timeout": 120}


def test_the_merge_script_sends_the_same_command_with_the_same_wait_and_reads_failures_as_before(default_mode):
    merge = load("merge_default", "merge-pr-dispatch.py")
    refused = json.dumps({"outcome": "rpc_send_text_timeout", "rpc_send_text": {"body": "timeout"}})
    for authorized_by in ("tap", "standing"):
        with mock.patch.object(merge.subprocess, "run",
                               return_value=mock.Mock(returncode=5, stdout=refused, stderr="")) as ran:
            ok, why = merge.step2_send_and_verify(WS, "/merge-when-ready 7", *([authorized_by] if hasattr(
                merge, "_pro_mode") else []))
        assert ran.call_args.args == ([str(REPO / "bin/cmux-send.py"), "--ws", WS, "--text", "/merge-when-ready 7",
                                       "--enter", "--caller", "merge-pr-dispatch.py"],)
        assert ran.call_args.kwargs == {"capture_output": True, "text": True, "timeout": 20}
        assert (ok, why) == (False, "cmux_send_exit_5_outcome=rpc_send_text_timeout: timeout")
    effects_untouched(default_mode)


# --- bin/cmux-send.py ----------------------------------------------------------------------------------------------

SURFACE = ("S-1", "surface:1", "ttys00", "title")


def run_send(home, answers, *argv):
    send = load("cmux_send_default", "cmux-send.py")
    send.SENDS_LOG = str(home / ".assistant/sends.jsonl")
    calls = []

    def rpc(method, payload):
        calls.append((method, payload))
        return answers.pop(0) if answers else (0, {"ok": True})

    sys.argv = ["cmux-send.py", "--ws", WS, "--text", MARKER, "--post-send-wait", "0", *argv]
    out = io.StringIO()
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch.object(send, "rpc", rpc), mock.patch.object(send.time, "sleep"), \
            mock.patch("sys.stdout", out), pytest.raises(SystemExit) as stop:
        send.main()
    record = json.loads(out.getvalue())
    for volatile in ("ts", "caller_pid", "wall_ms"):
        record.pop(volatile, None)
    log = home / ".assistant/sends.jsonl"
    logged = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    for row in logged:
        for volatile in ("ts", "caller_pid", "wall_ms"):
            row.pop(volatile, None)
    return stop.value.code, record, logged, calls


BASE = {"caller": "cmux-send.py", "target_ws_ref": WS, "target_ws_title": "title", "target_surface_id": "S-1",
        "target_surface_ref": "surface:1", "target_tty": "ttys00", "text": MARKER, "send_enter": True,
        "transcript_path": None, "transcript_size_before": None}
TEXT = ("surface.send_text", {"surface_id": "S-1", "text": MARKER})
KEY = ("surface.send_key", {"surface_id": "S-1", "key": "enter"})


def test_the_send_script_on_success_prints_and_logs_what_it_always_did(default_mode):
    code, record, logged, calls = run_send(default_mode, [], "--enter")
    assert code == 0 and calls == [TEXT, KEY]
    assert record == {**BASE, "rpc_send_text": {"rc": 0, "body": {"ok": True}},
                      "rpc_send_key": {"rc": 0, "body": {"ok": True}}, "transcript_size_after": None,
                      "transcript_size_delta": 0, "outcome": "sent"}
    assert logged == [record]
    effects_untouched(default_mode)


def test_the_send_script_on_a_refusal_exits_2_and_does_not_press_enter(default_mode):
    code, record, logged, calls = run_send(default_mode, [(1, {"error": "no"})], "--enter")
    assert code == 2 and calls == [TEXT]
    assert record == {**BASE, "rpc_send_text": {"rc": 1, "body": {"error": "no"}}, "outcome": "rpc_send_text_failed",
                      "transcript_size_after": None, "transcript_size_delta": 0}
    assert logged == [record]
    effects_untouched(default_mode)


def test_the_send_script_reads_a_time_out_as_a_refusal_and_exits_2(default_mode):
    code, record, _logged, calls = run_send(default_mode, [(124, {"error": "timeout"})], "--enter")
    assert code == 2 and calls == [TEXT]
    assert record["outcome"] == "rpc_send_text_failed"
    assert record["rpc_send_text"] == {"rc": 124, "body": {"error": "timeout"}}
    code, record, _logged, calls = run_send(default_mode, [(0, {}), (124, {"error": "timeout"})], "--enter")
    assert code == 2 and calls == [TEXT, KEY] and record["outcome"] == "rpc_send_key_failed"
    effects_untouched(default_mode)


def test_the_send_script_takes_a_null_answer_with_exit_status_zero_as_sent_and_text_only_stops_there(default_mode):
    code, record, _logged, calls = run_send(default_mode, [(0, None)])
    assert code == 0 and calls == [TEXT] and record["rpc_send_text"] == {"rc": 0, "body": None}
    assert record["send_enter"] is False
    effects_untouched(default_mode)


def test_the_send_script_with_no_terminal_exits_1(default_mode):
    send = load("cmux_send_default2", "cmux-send.py")
    send.SENDS_LOG = str(default_mode / ".assistant/sends.jsonl")
    sys.argv = ["cmux-send.py", "--ws", WS, "--text", MARKER, "--no-log"]
    out = io.StringIO()
    with mock.patch.object(send, "resolve_terminal_surface", return_value=(None,) * 4), mock.patch("sys.stdout", out), \
            pytest.raises(SystemExit) as stop:
        send.main()
    assert stop.value.code == 1 and json.loads(out.getvalue())["outcome"] == "no_terminal_surface"
    effects_untouched(default_mode)


def test_the_send_script_never_refuses_a_loop_in_the_default_mode(default_mode):
    for _ in range(5):  # the fifth identical run
        code, _record, _logged, calls = run_send(default_mode, [], "--enter")
        assert code == 0 and calls == [TEXT, KEY]
    effects_untouched(default_mode)


def test_the_send_script_types_different_texts_in_the_default_mode_and_makes_no_digest_or_key(default_mode):
    """The text digest is Pro mode's: the default mode never loads it, so no secret is made and no record is."""
    for text in ("/merge-when-ready 7", "/merge-when-ready 8", "/merge-when-ready 9", "/merge-when-ready 10"):
        send = load("cmux_send_default3", "cmux-send.py")
        send.SENDS_LOG = str(default_mode / ".assistant/sends.jsonl")
        calls = []
        sys.argv = ["cmux-send.py", "--ws", WS, "--text", text, "--enter", "--post-send-wait", "0"]
        with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
                mock.patch.object(send, "rpc", lambda m, p: calls.append((m, p)) or (0, {"ok": True})), \
                mock.patch.object(send.time, "sleep"), mock.patch("sys.stdout", io.StringIO()), \
                pytest.raises(SystemExit) as stop:
            send.main()
        assert stop.value.code == 0 and calls[0] == ("surface.send_text", {"surface_id": "S-1", "text": text})
    effects_untouched(default_mode)
    assert not (default_mode / ".assistant/feedback").exists()


def test_the_pulse_types_into_a_task_with_an_auto_id_in_the_default_mode_and_records_nothing(default_mode):
    pulse = load("pulse_default_auto", "pulse.py")
    assert pulse._begin_dispatch_typing("td-auto-1a2b3c4d", {}, WS, "task") == pulse.DEFAULT_TYPING
    effects_untouched(default_mode)


# --- the Slack session ------------------------------------------------------------------------------------------

@pytest.fixture
def listen(default_mode, monkeypatch):
    import comms_lib as cl
    monkeypatch.setenv("COMMS_HOME", str(default_mode))
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    (default_mode / ".assistant/comms").mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("comms_listen_default_mode", REPO / "bin/comms-listen.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["comms_listen_default_mode"] = module
    spec.loader.exec_module(module)
    sent: list = []

    def cli(argv, timeout=30, env=None):
        sent.append(argv)
        return 0, "{}", ""

    monkeypatch.setattr(module, "cli", cli)
    module.sent, module.cl = sent, cl
    return module


def config(home, target):
    (home / ".assistant/config.json").write_text(
        json.dumps({"slack": {"target": target, "allowed_targets": [target]}}))


def slack_texts(sent):
    return [(a[a.index("--channel") + 1], a[a.index("--text") + 1],
             a[a.index("--reply-to") + 1] if "--reply-to" in a else None) for a in sent if "slack-send.py" in a[0]]


def run_inbound(listen, messages, monkeypatch):
    paths = listen.comms_lib.Paths.from_env()
    monkeypatch.setattr(listen, "ensure_warm_session", lambda p, **kw: None)
    stop = threading.Event()
    woke = []

    def worker(channel_id, wake, stop_, env_):
        woke.append(channel_id)
        wake.get(timeout=5)
        stop_.set()

    def poll(stop_, env_, msg_queue):
        for message in messages:
            msg_queue.put(message)

    class QuickQueue(listen.queue.Queue):
        def get(self, block=True, timeout=None):
            return super().get(block, 0.02 if timeout == 1 else timeout)

    monkeypatch.setattr(listen.queue, "Queue", QuickQueue)
    monkeypatch.setattr(listen, "_channel_worker", worker)
    monkeypatch.setattr(listen, "_poll_thread", poll)
    t = threading.Thread(target=listen.inbound_loop, args=(stop, {}), daemon=True)
    t.start()
    t.join(timeout=10)
    return paths, woke


@pytest.mark.parametrize("target, author, owner", [
    ("C0CHAN", "U0OTHER", ""), ("C0CHAN", "U0OWNER", ""), ("C0CHAN", "", ""), ("U0OWNER", "U0OTHER", ""),
    ("D0DIRECT01", "U0OWNER", ""),
    ("C0CHAN", "U0OTHER", "U0OWNER"), ("C0CHAN", "", "U0OWNER")])  # with the owner named, still anyone's
def test_every_message_from_the_conversation_is_queued_to_be_typed_whoever_wrote_it_and_nobody_is_told(
        listen, default_mode, monkeypatch, target, author, owner):
    config(default_mode, target)
    if owner:
        monkeypatch.setenv("SLACK_USER_ID", owner)
    message = {"channel": "C0CHAN", "msg_ts": "1793613610.000100", "author": author, "text": "hello", "reply_to": None}
    paths, woke = run_inbound(listen, [message], monkeypatch)
    assert [r["text"] for r in listen.read_pending(paths)] == ["hello"] and woke == ["C0CHAN"]
    assert slack_texts(listen.sent) == []  # no notice to anyone: only the conversation record was written
    assert [a[a.index("--text") + 1] for a in listen.sent if "conversation.py" in a[0] and "append" in a] == ["hello"]
    effects_untouched(default_mode)


class Session:
    def __init__(self, listen, monkeypatch, landed=True, timed_out=False):
        self.cs, self.calls, self.listen, self.timed_out = listen.comms_session, [], listen, timed_out
        cs = self.cs
        monkeypatch.setattr(cs, "_cmux_rpc", self.rpc)
        monkeypatch.setattr(cs.time, "sleep", lambda *_a: None)
        monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=200: "─" * 20 + "\n❯ \n" + "─" * 20)
        monkeypatch.setattr(cs, "should_clear", lambda *a, **k: False)
        monkeypatch.setattr(cs, "transcript_has_submission", lambda path, marker: landed and len(self.calls) >= 2)
        monkeypatch.setattr(cs, "find_submission", lambda *a, **k: None)
        monkeypatch.setattr(cs, "write_session", lambda *a, **k: None)
        monkeypatch.setattr(cs, "read_session", lambda paths: None)
        defaults = (cs.SUBMIT_ATTEMPTS, 0.0, lambda s: None, cs.time.monotonic, lambda: False, 0.0)
        monkeypatch.setattr(cs.submit_until_confirmed, "__defaults__",
                            defaults + ((False,) if len(cs.submit_until_confirmed.__defaults__) > 6 else ()))

    def rpc(self, paths, method, params, timeout=15):
        if method.startswith("surface.send"):
            self.calls.append((method, params))
            if self.timed_out:  # what the real call leaves behind when cmux times out (nothing to leave on main)
                if hasattr(self.listen.comms_lib, "clear_last_run"):
                    self.listen.comms_lib._last.result = (-1, "timeout after 15s")
                return None
        return {}


SESS = {"ws_ref": WS, "surface_ref": "surface:3", "cwd": "/cwd", "agent": "claude", "transcript_path": "/t.jsonl"}
OTHER_MESSAGE = [{"channel": "C0", "text": "hello", "msg_ts": "1793613610.000100", "reply_to": None, "author": "U0OTHER"}]


def test_a_message_from_anyone_is_typed_into_the_session_with_its_header_then_a_carriage_return(
        listen, default_mode, monkeypatch):
    session = Session(listen, monkeypatch)
    delivered, _ = listen.reply_to_message(listen.comms_lib.Paths.from_env(), dict(SESS), OTHER_MESSAGE)
    assert delivered is True and len(session.calls) == 2
    text = session.calls[0]
    assert text[0] == "surface.send_text" and text[1]["surface_id"] == "surface:3"
    assert text[1]["text"].startswith("[slack channel=C0 msg_ts=1793613610.000100 send_cli=")
    assert text[1]["text"].endswith("] hello")
    assert session.calls[1] == ("surface.send_text", {"surface_id": "surface:3", "text": "\r"})
    effects_untouched(default_mode)


def test_a_time_out_typing_a_message_is_ignored_as_before_and_the_carriage_return_still_follows(
        listen, default_mode, monkeypatch):
    session = Session(listen, monkeypatch, landed=False, timed_out=True)
    delivered, _ = listen.reply_to_message(listen.comms_lib.Paths.from_env(), dict(SESS), OTHER_MESSAGE)
    assert delivered is False and session.calls[0][1]["text"].endswith("] hello")
    assert {c[1]["text"] for c in session.calls[1:]} == {"\r"}
    effects_untouched(default_mode)


def test_a_message_typed_but_not_seen_is_not_confirmed_and_nothing_is_recorded(listen, default_mode, monkeypatch):
    session = Session(listen, monkeypatch, landed=False)
    delivered, _ = listen.reply_to_message(listen.comms_lib.Paths.from_env(), dict(SESS), OTHER_MESSAGE)
    assert delivered is False and session.calls[0][1]["text"].endswith("] hello")
    assert {c[1]["text"] for c in session.calls[1:]} == {"\r"}
    effects_untouched(default_mode)


def test_the_back_soon_note_goes_to_the_conversation_the_message_came_in_on_even_for_a_person_target(
        listen, default_mode):
    config(default_mode, "U0OWNER")
    paths = listen.comms_lib.Paths.from_env()
    listen._notify_restart_once(paths, "D0DIRECT01", None)
    assert slack_texts(listen.sent) == [("D0DIRECT01", listen.RESTART_NOTICE, None)]
    effects_untouched(default_mode)


def test_a_message_waiting_for_a_session_that_isnt_responding_gets_the_old_note_and_stays_queued(
        listen, default_mode, monkeypatch):
    config(default_mode, "C0CHAN")
    paths = listen.comms_lib.Paths.from_env()
    listen.add_pending(paths, {"channel": "C0", "text": "hi", "msg_ts": f"{time.time() - 600:.6f}", "author": "U0OTHER"})
    monkeypatch.setattr(listen, "_warm_session", lambda p, **kw: (None, listen.SESSION_NONE))
    assert listen._deliver_pending(paths, "C0", None) is False
    assert [r["text"] for r in listen.read_pending(paths)] == ["hi"]
    assert slack_texts(listen.sent) == [("C0", listen.RESTART_NOTICE, None)]
    effects_untouched(default_mode)


def test_messages_queued_by_anyone_are_delivered_and_an_expired_one_is_dropped_without_a_word(
        listen, default_mode, monkeypatch):
    config(default_mode, "C0CHAN")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")  # even with the owner known, the default mode types anyone's
    paths = listen.comms_lib.Paths.from_env()
    session = Session(listen, monkeypatch)
    listen.add_pending(paths, {"channel": "C0", "text": "queued", "msg_ts": f"{time.time():.6f}", "author": "U0OTHER"})
    listen.add_pending(paths, {"channel": "C0", "text": "old", "msg_ts": f"{time.time() - 4 * 3600:.6f}",
                               "author": "U0OTHER"})
    monkeypatch.setattr(listen, "_warm_session", lambda p, **kw: (dict(SESS), listen.SESSION_ALIVE))
    assert listen._deliver_pending(paths, "C0", None) is True
    assert listen.read_pending(paths) == [] and len(session.calls) == 2
    assert slack_texts(listen.sent) == []
    effects_untouched(default_mode)


def test_the_slack_session_types_without_any_scope_as_it_always_could(listen, default_mode, monkeypatch):
    session = Session(listen, monkeypatch)
    cs = session.cs
    paths = listen.comms_lib.Paths.from_env()
    cs.send_enter(paths, "surface:9")
    assert cs.submit(paths, "surface:9", "hello msg_ts=5\n", "msg_ts=5", lambda: len(session.calls) >= 3)
    assert session.calls == [("surface.send_text", {"surface_id": "surface:9", "text": "\r"}),
                             ("surface.send_text", {"surface_id": "surface:9", "text": "hello msg_ts=5"}),
                             ("surface.send_text", {"surface_id": "surface:9", "text": "\r"})]
    effects_untouched(default_mode)


def test_the_slack_sessions_start_up_types_the_trust_answer_and_clear_types_clear_then_the_boot_prompt(
        listen, default_mode, monkeypatch, tmp_path):
    from test_comms_session import _spawn_env
    cs = listen.comms_session
    (tmp_path / "spawn").mkdir()
    paths, closed = _spawn_env(tmp_path / "spawn", monkeypatch, ws=WS, surface="surface:310")
    calls = []
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, m, params, timeout=15: calls.append((m, params.get("text"))) or {})
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    monkeypatch.setattr(cs, "await_ready", lambda **kw: (kw["answer_trust"](), (True, True))[1])
    monkeypatch.setattr(cs, "deliver_boot", lambda p, surface, cwd, prompt, agent, submit_fn=None: "/t.jsonl")
    assert cs.spawn_session(paths, Path("/boot.md"), agent="claude")["transcript_path"] == "/t.jsonl"
    assert calls == [("surface.send_text", "1"), ("surface.send_text", "\r")]
    calls.clear()
    monkeypatch.setattr(cs, "_surface_read_text", lambda *a, **k: "Welcome back!\n" + "─" * 40 + "\n❯ \n" + "─" * 40)
    monkeypatch.setattr(cs, "deliver_boot", lambda p, surface, cwd, prompt, agent, submit_fn=None: "/new.jsonl")
    cs.write_session(paths, WS, "surface:3", "/cwd", "/old.jsonl")
    cs.clear_session(paths, {"ws_ref": WS, "surface_ref": "surface:3", "cwd": "/cwd"}, Path("/boot.md"), agent="claude")
    assert calls == [("surface.send_text", "/clear"), ("surface.send_text", "\r")]
    effects_untouched(default_mode)


# --- the dashboard's "run now" ----------------------------------------------------------------------------------

def _server():
    from test_todo_server import mod  # noqa: PLC0415 - the server module the file's own tests use
    return mod


def run_now(env, action, ref, execute_via="send-text-to-session"):
    from test_todo_server import _seed_proposal
    path = _seed_proposal(env, {"id": "prop-1", "execute_via": execute_via, "action": action,
                                "touches": [{"type": "session", "ref": ref}]})
    server = _server()
    calls = []
    with mock.patch.object(server.subprocess, "run", lambda argv, **kw: calls.append((argv, kw))):
        result = server.proposal_action("prop-1", "run-now", {})
    return result, calls, json.loads(path.read_text())


@pytest.mark.parametrize("action, ref", [
    ("do the thing", WS), ("do the thing", "workspace:7"),
    ("do the thing", "not-a-session"),               # a ref that isn't workspace:N
    ("do the thing", "workspace:" + "1" * 19),
    ("one\ntwo", WS), ("x" * 501, WS), ("a\x03b", WS), ("a b", WS), ("", WS)])
def test_run_now_types_what_a_proposal_says_into_the_session_it_names_with_cmux_send_whatever_it_is(
        env, default_mode, action, ref):
    result, calls, saved = run_now(env, action, ref)
    cmux = _server().CMUX_BIN
    kw = {"check": False, "timeout": 4, "capture_output": True}
    assert result == (True, "prop-1.run-now ok")
    assert calls == [([cmux, "send", "--workspace", ref, action], kw), ([cmux, "send-key", "--workspace", ref, "Enter"], kw)]
    assert saved["status"] == "working" and saved["thread"][-1]["note"] == "Dispatched via dashboard Run button."
    effects_untouched(default_mode)


def test_run_now_with_a_missing_action_types_an_empty_line_and_other_executors_type_nothing(env, default_mode):
    from test_todo_server import _seed_proposal
    _seed_proposal(env, {"id": "prop-2", "execute_via": "send-text-to-session",
                         "touches": [{"type": "session", "ref": WS}]})
    server, calls = _server(), []
    with mock.patch.object(server.subprocess, "run", lambda argv, **kw: calls.append(argv)):
        assert server.proposal_action("prop-2", "run-now", {}) == (True, "prop-2.run-now ok")
    assert [c[1:] for c in calls] == [["send", "--workspace", WS, ""], ["send-key", "--workspace", WS, "Enter"]]
    result, calls, _saved = run_now(env, "x", WS, execute_via="manual")
    assert result == (True, "prop-1.run-now ok") and calls == []


def test_run_now_reports_a_script_that_cant_run_as_before(env, default_mode):
    from test_todo_server import _seed_proposal
    _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": "x",
                         "touches": [{"type": "session", "ref": WS}]})
    server = _server()
    with mock.patch.object(server.subprocess, "run", side_effect=RuntimeError("cmux down")):
        assert server.proposal_action("prop-1", "run-now", {}) == (False, "cmux send failed: cmux down")


def test_queue_to_start_leaves_a_park_marker_alone_in_the_default_mode(env, default_mode):
    server = _server()
    server.save_json({"items": [{"id": "td-1", "status": "blocked", "parkedBy": "typing_refused",
                                 "dispatchedAt": "x", "dispatchedWs": WS}]})
    assert server.dispatch_now("td-1") == (True, "td-1: queued for next-pulse Bucket B dispatch")
    task = server.load_json()["items"][0]
    assert (task["status"], task["dispatchedWs"], task.get("parkedBy")) == ("open", None, "typing_refused")


# --- restore -------------------------------------------------------------------------------------------------------

def restore_module():
    return load("restore_default_mode", "cmux-restore-sessions.py")


PANEL = {"id": "panel-1", "terminal": {"surfaceId": "surface-1"}}


def respawn(restore, answers, verbose=False):
    calls = []

    def run_cmux(*args, timeout=5):
        calls.append((args[1], json.loads(args[2]), timeout))
        return answers.pop(0)

    restore.run_cmux = run_cmux
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = restore.respawn_panel({"title": "t"}, PANEL, "cd /x && claude --resume abc", False, verbose)
    return result, calls, out.getvalue()


def test_restore_types_the_command_and_presses_enter_whatever_the_terminal_id_looks_like(default_mode):
    result, calls, _out = respawn(restore_module(), [(0, "{}", ""), (1, "", "enter refused")])
    assert result is True and calls == [
        ("surface.send_text", {"surface_id": "surface-1", "text": "cd /x && claude --resume abc"}, 5),
        ("surface.send_key", {"surface_id": "surface-1", "key": "enter"}, 5)]
    effects_untouched(default_mode)


def test_restore_tries_the_panels_id_when_the_first_try_fails_and_says_what_cmux_said(default_mode):
    result, calls, out = respawn(restore_module(), [(1, "", "no such surface"), (1, "", "still no")], verbose=True)
    assert result is False and [c[1]["surface_id"] for c in calls] == ["surface-1", "panel-1"]
    assert out.splitlines()[-1] == "  ERROR: send_text failed: still no"
    result, calls, _out = respawn(restore_module(), [(1, "", "x"), (0, "{}", ""), (0, "{}", "")])
    assert result is True and [c[1].get("surface_id") for c in calls] == ["surface-1", "panel-1", "surface-1"]


def test_a_time_out_answer_from_cmux_in_restore_is_a_refusal_that_is_tried_again_with_the_panels_id(default_mode):
    result, calls, _out = respawn(restore_module(), [(124, "", "t"), (0, "{}", ""), (0, "{}", "")])
    assert result is True and [c[1].get("surface_id") for c in calls] == ["surface-1", "panel-1", "surface-1"]
    result, calls, out = respawn(restore_module(), [(124, "", "t"), (124, "", "t")], verbose=True)
    assert result is False and out.splitlines()[-1] == "  ERROR: send_text failed: t"
    effects_untouched(default_mode)


def test_a_cmux_time_out_in_restore_is_a_failure_with_the_exceptions_own_words(default_mode):
    restore = restore_module()
    out = io.StringIO()
    with mock.patch.object(restore.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="cmux", timeout=5)), \
            contextlib.redirect_stdout(out):
        assert restore.respawn_panel({"title": "t"}, PANEL, "cd /x", False, True) is False
    assert out.getvalue().splitlines()[-1] == "  ERROR: send_text failed: Command 'cmux' timed out after 5 seconds"
    effects_untouched(default_mode)


# --- the doctor and the dashboard -----------------------------------------------------------------------------------

def test_the_doctor_runs_the_slack_checks_it_always_ran(default_mode, monkeypatch):
    sys.path.insert(0, str(REPO / "bin"))
    spec = importlib.util.spec_from_file_location("assistant_doctor_default", REPO / "bin/assistant-doctor.py")
    doctor = importlib.util.module_from_spec(spec)
    sys.modules["assistant_doctor_default"] = doctor
    spec.loader.exec_module(doctor)
    config(default_mode, "C0CHAN")
    stub = lambda: doctor.Check("slack token", doctor.SKIP, core=False)  # noqa: E731
    owner = [getattr(doctor, "check_slack_owner")] if hasattr(doctor, "check_slack_owner") else []
    monkeypatch.setattr(doctor, "SLACK_CHECKS", [stub, *owner])
    assert [c.name for c in doctor.run_checks(only="slack")] == ["slack token"]


def render(default_mode, state, todos=None):
    (default_mode / ".claude/cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
    (default_mode / ".claude/cache/assistant-state.json").write_text(json.dumps(state))
    if todos is not None:
        (default_mode / ".claude/assistant-todo.json").write_text(json.dumps({"items": todos}))
    spec = importlib.util.spec_from_file_location("render_default_mode", REPO / "bin/render-assistant-page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.render()
    return (default_mode / ".claude/assistant-dashboard.html").read_text()


def test_the_tasks_tab_shows_no_card_for_a_refused_typing_and_the_old_cards_keep_their_backticks(default_mode):
    page = render(default_mode, {"awaiting_input": [
        {"key": "dispatch-held-no-record", "title": "Dispatch paused — can't record what it types (2 TODO(s) waiting)",
         "detail": "Check `df -h ~`."}]})
    assert "can&#x27;t record what it types" not in page[page.index('data-panel="todos"'):]
    page = render(default_mode, {"awaiting_input": [
        {"key": "dispatch-cap-hit", "title": "Dispatch capped — 2 TODO(s) waiting", "detail": "Holding `td-1`."}]})
    todos = page[page.index('data-panel="todos"'):]
    assert "Holding `td-1`." in todos and "<code>td-1</code>" not in todos


def test_a_blocked_or_parked_task_row_looks_as_it_always_did(default_mode):
    parked = {"id": "td-1", "title": "Fix it", "priority": "P1", "status": "blocked", "autoDispatch": True,
              "dispatchedAt": "2026-11-24T10:00:00Z", "dispatchedWs": WS, "parkedBy": "typing_refused",
              "statusReason": "Parked: because."}
    page = render(default_mode, {}, [parked])
    row = page[page.index('data-task-id="td-1"'):]
    assert ">dispatched</span>" in row and 'title="Parked: because."' not in row
    assert '<span class="pill status-blocked">blocked</span>' in row


# --- the options of the two scripts ---------------------------------------------------------------------------------

CONFIGS = [None, '{"mode": "default"}', "{ this is not json"]  # no file, the default mode, a file nobody can read


def write_config(home, text):
    if text is not None:
        (home / ".assistant/config.json").write_text(text)


def run_script(script, argv, columns="80"):
    module = load("parser_" + script.replace("-", "_").replace(".py", ""), script)
    sys.argv = [script, *argv]
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict("os.environ", {"COLUMNS": columns}), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err), \
            mock.patch.object(module, "resolve_terminal_surface", create=True, return_value=("S", "surface:1", "tty", "t")), \
            mock.patch.object(module, "rpc", create=True, return_value=(0, {})), pytest.raises(SystemExit) as stop:
        module.main()
    return stop.value.code, out.getvalue(), err.getvalue()


SEND_USAGE = ("usage: cmux-send.py [-h] --ws WS --text TEXT [--enter] [--caller CALLER]\n"
              "                    [--no-log] [--post-send-wait POST_SEND_WAIT]\n")
MERGE_USAGE = ("usage: merge-pr-dispatch.py [-h] --ws WS --pr PR [--refactor-attested]\n"
               "                            [--e2e-attested]\n")


@pytest.mark.real_mode
@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("extra", [["--source", "merge"], ["--auth-kind", "tap"], ["--auth-ref", "x"],
                                   ["--task", "td-1"]])
def test_the_send_script_rejects_the_options_only_pro_mode_has(default_mode, config, extra):
    write_config(default_mode, config)
    code, out, err = run_script("cmux-send.py", ["--ws", WS, "--text", "x", "--no-log", *extra])
    assert (code, out) == (2, "")
    assert err == SEND_USAGE + f"cmux-send.py: error: unrecognized arguments: {' '.join(extra)}\n"
    effects_untouched(default_mode)


@pytest.mark.real_mode
@pytest.mark.parametrize("config", CONFIGS)
def test_the_send_script_reads_a_shortened_text_option_as_it_always_did(default_mode, config):
    write_config(default_mode, config)
    code, out, err = run_script("cmux-send.py", ["--ws", WS, "--t", "hello", "--no-log", "--post-send-wait", "0"])
    record = json.loads(out)
    assert (code, err, record["text"], record["outcome"]) == (0, "", "hello", "sent")
    effects_untouched(default_mode)


SEND_HELP = (SEND_USAGE + "\noptions:\n  -h, --help            show this help message and exit\n"
             "  --ws WS               workspace:N\n  --text TEXT           literal text to send\n"
             "  --enter               press Enter after sending text (default: do not press\n"
             "                        Enter)\n  --caller CALLER       who is calling — recorded in the log\n"
             "  --no-log              do not append to sends.jsonl (for debug runs)\n"
             "  --post-send-wait POST_SEND_WAIT\n"
             "                        seconds to wait before re-stat'ing the transcript for\n"
             "                        the size-delta proof (default 2.5)\n")
MERGE_HELP = (MERGE_USAGE + "\noptions:\n  -h, --help           show this help message and exit\n"
              "  --ws WS              workspace:N\n  --pr PR\n"
              "  --refactor-attested  Dispatcher claims the workspace transcript shows full\n"
              "                       local G3 + unit suite green\n"
              "  --e2e-attested       Dispatcher claims the workspace transcript shows pnpm\n"
              "                       e2e:squirrel passed (required for Squirrel PRs)\n")


@pytest.mark.real_mode
@pytest.mark.parametrize("config", CONFIGS)
def test_the_help_of_both_scripts_is_what_it_always_was(default_mode, config):
    write_config(default_mode, config)
    assert run_script("cmux-send.py", ["--help"]) == (0, SEND_HELP, "")
    assert run_script("merge-pr-dispatch.py", ["--help"]) == (0, MERGE_HELP, "")


@pytest.mark.real_mode
@pytest.mark.parametrize("config", CONFIGS)
def test_the_merge_script_rejects_the_option_only_pro_mode_has(default_mode, config):
    write_config(default_mode, config)
    code, out, err = run_script("merge-pr-dispatch.py", ["--ws", WS, "--pr", "7", "--authorized-by", "standing"])
    assert (code, out) == (2, "")
    assert err == MERGE_USAGE + "merge-pr-dispatch.py: error: unrecognized arguments: --authorized-by standing\n"
