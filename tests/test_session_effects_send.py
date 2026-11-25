"""bin/cmux-send.py records every typing in the effects ledger; its two callers say what allowed it."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from assistant import effects

from test_send_dispatch_in_process import fixture_home, load_module
from test_pulse_topup import load_pulse

MARKER = "TEXT-MARKER-do-not-record"
SURFACE = ("S-1", "surface:1", "ttys00", "title")


@pytest.fixture()
def home(tmp_path):
    effects._forget()
    return fixture_home(tmp_path)


@pytest.fixture()
def send(home):
    return load_module(home, "cmux-send.py")


def ledger(home) -> list:
    path = home / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Rpc:
    def __init__(self, home, answers=None):
        self.home, self.answers, self.calls, self.first = home, list(answers or []), [], None

    def __call__(self, method, payload):
        if self.first is None:
            self.first = ledger(self.home)
        self.calls.append((method, payload))
        return self.answers.pop(0) if self.answers else (0, {"ok": True})


def flags_of(cmd) -> dict:
    return {word: cmd[i + 1] for i, word in enumerate(cmd[:-1]) if word.startswith("--")}


def run(send, rpc, *argv, text=MARKER):
    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", text, "--no-log", "--post-send-wait", "0", *argv]
    out = io.StringIO()
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch.object(send, "rpc", rpc), mock.patch.object(send.time, "sleep"), \
            mock.patch("sys.stdout", out), pytest.raises(SystemExit) as stop:
        send.main()
    return stop.value.code, json.loads(out.getvalue())


def test_the_intent_is_on_disk_before_the_first_keystroke_and_one_effect_is_made(send, home):
    rpc = Rpc(home)
    code, _ = run(send, rpc, "--enter", "--source", "cleanup", "--auth-kind", "standing", "--auth-ref", "cleanup",
                  "--caller", "assistant-pulse")
    assert code == 0
    assert [r["kind"] for r in rpc.first] == ["intent"]
    rows = ledger(home)
    assert [r["kind"] for r in rows] == ["intent", "result"]
    assert rows[0]["target"] == {"source": "cleanup", "session": "workspace:1000010005"}
    assert rows[0]["auth"]["kind"] == "standing" and rows[0]["auth"]["ref"] == "cleanup"
    assert rows[0]["component"] == "assistant-pulse"
    assert rows[1]["outcome"] == "done"
    assert MARKER not in (home / ".assistant/effects/effects.jsonl").read_text()


def test_run_by_hand_it_counts_as_a_persons_tap_and_the_task_is_recorded(send, home):
    code, _ = run(send, Rpc(home), "--task", "td-9")
    row = ledger(home)[0]
    assert code == 0 and row["auth"]["kind"] == "tap" and row["auth"]["ref"] == "cli:cmux-send"
    assert row["target"] == {"source": "cli", "session": "workspace:1000010005", "task": "td-9"}


def test_a_transcript_that_grew_is_verified(send, home):
    transcript = home / "t.jsonl"
    transcript.write_text("a\n")
    sizes = iter([2, 9])
    with mock.patch.object(send, "find_transcript_for_ws", return_value=str(transcript)), \
            mock.patch.object(send, "file_size", side_effect=lambda p: next(sizes)):
        run(send, Rpc(home))
    assert ledger(home)[1]["verified"] is True


@pytest.mark.parametrize("answers, args, code, outcome", [
    ([(1, {"error": "no"})], ["--enter"], 2, "failed"),                      # nothing typed
    ([(0, {}), (1, {"error": "no"})], ["--enter"], 2, "unknown"),            # the text is there, Enter isn't
    ([(124, {"error": "timeout"})], ["--enter"], 5, "unknown"),              # can't tell: its own exit status
    ([(0, {}), (124, {"error": "timeout"})], ["--enter"], 5, "unknown"),
    ([(0, {})], [], 0, "done"),                                              # text only, as asked
])
def test_the_result_says_what_happened(send, home, answers, args, code, outcome):
    got, _ = run(send, Rpc(home, answers), *args)
    assert got == code and ledger(home)[1]["outcome"] == outcome


def test_with_the_ledger_unwritable_nothing_is_typed_and_it_exits_4(send, home, monkeypatch):
    def broken(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(effects, "_write", broken)
    rpc = Rpc(home)
    code, record = run(send, rpc, "--enter")
    assert code == 4 and rpc.calls == [] and record["outcome"] == "refused"
    assert record["refused"]["code"] == "unrecorded" and "disk" in record["refused"]["reason"]


def test_the_fourth_identical_typing_in_half_an_hour_is_refused_with_nothing_typed(send, home):
    for _ in range(3):
        assert run(send, Rpc(home), "--enter", "--source", "nudge:stranded", "--auth-kind", "standing",
                   "--auth-ref", "nudge:stranded")[0] == 0
    rpc = Rpc(home)
    code, record = run(send, rpc, "--enter", "--source", "nudge:stranded", "--auth-kind", "standing",
                       "--auth-ref", "nudge:stranded")
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "loop"


def test_a_typing_that_may_have_landed_is_not_repeated(send, home):
    run(send, Rpc(home, [(0, {}), (124, {})]), "--enter", "--source", "cleanup", "--auth-kind", "standing")
    rpc = Rpc(home)
    code, record = run(send, rpc, "--enter", "--source", "cleanup", "--auth-kind", "standing")
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "unknown"


def test_a_bad_workspace_or_no_terminal_makes_no_effect(send, home):
    sys.argv = ["cmux-send.py", "--ws", "workspace:x", "--text", "hi", "--no-log"]
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch("sys.stdout", io.StringIO()), pytest.raises(SystemExit) as stop:
        send.main()
    assert stop.value.code == 4 and ledger(home) == []
    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", "hi", "--no-log"]
    with mock.patch.object(send, "resolve_terminal_surface", return_value=(None,) * 4), \
            mock.patch("sys.stdout", io.StringIO()), pytest.raises(SystemExit) as stop:
        send.main()
    assert stop.value.code == 1 and ledger(home) == []


# --- the callers ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, source", [("/cleanup", "cleanup"), ("STRANDED", "nudge:stranded"), ("other", "pulse")])
def test_the_pulse_says_it_is_the_standing_setting_and_which_fixed_nudge(home, text, source):
    pulse = load_pulse(home)
    text = pulse.SAFE_STRANDED_NUDGE if text == "STRANDED" else text
    with mock.patch.object(pulse, "run", return_value=(0, json.dumps({"outcome": "sent"}), "")) as ran:
        pulse.cmux_send("workspace:1000010003", text)
    cmd = ran.call_args.args[0]
    flags = flags_of(cmd)
    assert flags["--source"] == source and flags["--auth-kind"] == "standing" and flags["--auth-ref"] == source


def test_a_refused_typing_is_a_send_that_failed_for_the_pulse(home):
    pulse = load_pulse(home)
    with mock.patch.object(pulse, "run", return_value=(4, "{}", "cmux-send: Assistant couldn't record")):
        assert pulse.cmux_send("workspace:1000010003", "/cleanup") == {"outcome": "failed", "transcript_size_delta": 0}


def test_merge_dispatch_says_who_allowed_it_and_reports_a_refusal(home):
    merge = load_module(home, "merge-pr-dispatch.py")
    refused = json.dumps({"outcome": "refused", "refused": {"reason": "Assistant couldn't record it"}})
    with mock.patch.object(merge.subprocess, "run",
                           return_value=mock.Mock(returncode=4, stdout=refused, stderr="")) as ran:
        ok, why = merge.step2_send_and_verify("workspace:1000010002", "/merge-when-ready 7", "standing")
    cmd = ran.call_args.args[0]
    flags = flags_of(cmd)
    assert (ok, flags["--source"], flags["--auth-kind"], flags["--auth-ref"]) == (False, "merge", "standing", "merge")
    assert "couldn't record it" in why
    with mock.patch.object(merge.subprocess, "run", return_value=mock.Mock(returncode=4, stdout=refused, stderr="")) as ran:
        merge.step2_send_and_verify("workspace:1000010002", "/merge-when-ready 7")
    assert "tap" in ran.call_args.args[0]


def test_the_pulse_runs_merge_dispatch_as_standing_and_a_person_as_a_tap(home):
    pulse = load_pulse(home)
    with mock.patch.object(pulse.subprocess, "run",
                           return_value=mock.Mock(returncode=0, stdout=json.dumps({"outcome": "submitted"}))) as ran:
        pulse.run_merge_pr_dispatch("workspace:1000010001", 7, False, False)
    cmd = ran.call_args.args[0]
    assert cmd[cmd.index("--authorized-by") + 1] == "standing"
    merge = load_module(home, "merge-pr-dispatch.py")
    sys.argv = ["merge-pr-dispatch.py", "--ws", "workspace:1000010001", "--pr", "7"]
    seen = {}
    with mock.patch.object(merge, "step0_safety_gate", return_value=(True, "", {})), \
            mock.patch.object(merge, "gh_pr_view", return_value={"files": []}), \
            mock.patch.object(merge, "squirrel_e2e_gate", return_value=(True, "")), \
            mock.patch.object(merge, "step_freeze_retarget", return_value=(False, "", {})), \
            mock.patch.object(merge, "step1_ci_route", return_value=("merge-when-ready", "", {})), \
            mock.patch.object(merge, "step2_send_and_verify",
                              side_effect=lambda ws, slash, by: seen.update(by=by) or (True, "x")), \
            mock.patch("sys.stdout", io.StringIO()), pytest.raises(SystemExit):
        merge.main()
    assert seen["by"] == "tap"


def test_a_typing_refused_before_anything_was_sent_leaves_no_row_in_the_send_log(send, home, monkeypatch):
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    send.SENDS_LOG = str(home / "sends.jsonl")
    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", MARKER, "--enter"]
    out = io.StringIO()
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch("sys.stdout", out), pytest.raises(SystemExit) as stop:
        send.main()
    assert stop.value.code == 4 and json.loads(out.getvalue())["outcome"] == "refused"
    assert not (home / "sends.jsonl").exists()


def test_after_a_refusal_the_pulse_does_not_take_the_nudge_for_one_that_never_landed(send, home, monkeypatch):
    """pulse.previous_send_ingested reads sends.jsonl: a refused send used to leave a row with a zero delta, and the
    same nudge was then skipped for good, even after the cause was fixed."""
    pulse = load_pulse(home)
    log = home / ".assistant/sends.jsonl"
    send.SENDS_LOG = str(log)
    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", "/cleanup", "--enter", "--source", "cleanup",
                "--auth-kind", "standing"]
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch("sys.stdout", io.StringIO()), pytest.raises(SystemExit):
        with monkeypatch.context() as broken:
            broken.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
            send.main()
    pulse.SENDS_LOG = log
    assert pulse.previous_send_ingested("workspace:1000010005", "/cleanup") is True


def test_the_effect_is_closed_whatever_goes_wrong_after_it_was_begun(send, home):
    class Boom(Exception):
        pass

    def rpc(method, payload):
        raise Boom()

    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", MARKER, "--enter", "--no-log"]
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch.object(send, "rpc", rpc), mock.patch("sys.stdout", io.StringIO()), pytest.raises(Boom):
        send.main()
    assert ledger(home)[1]["outcome"] == "unknown"


def test_a_caller_name_with_a_space_is_still_accepted(send, home):
    code, _ = run(send, Rpc(home), "--enter", "--caller", "my caller.py")
    assert code == 0 and ledger(home)[0]["component"] == "my-caller.py"


def test_a_null_or_empty_answer_with_exit_status_zero_is_not_a_refusal(send, home):
    rpc = Rpc(home, [(0, None), (0, "")])
    code, _ = run(send, rpc, "--enter")
    assert code == 0 and [c[0] for c in rpc.calls] == ["surface.send_text", "surface.send_key"]
    assert ledger(home)[1]["outcome"] == "done"


def test_the_callers_wait_longer_than_the_script_can_take(home):
    """The script's worst case is its terminal lookup (10 s), two keystrokes (10 s each) and its 2.5 s wait."""
    worst = 10 + 10 + 10 + 2.5
    pulse = load_pulse(home)
    with mock.patch.object(pulse, "run", return_value=(0, json.dumps({"outcome": "sent"}), "")) as ran:
        pulse.cmux_send("workspace:1000010003", "/cleanup")
    assert ran.call_args.kwargs["timeout"] > worst
    merge = load_module(home, "merge-pr-dispatch.py")
    with mock.patch.object(merge.subprocess, "run", return_value=mock.Mock(returncode=4, stdout="{}", stderr="")) as ran:
        merge.step2_send_and_verify("workspace:1000010002", "/merge-when-ready 7")
    assert ran.call_args.kwargs["timeout"] > worst
    todo = load_module(home, "todo-server.py")
    assert todo.SEND_SCRIPT_TIMEOUT_SEC > worst


def test_a_transcript_that_did_not_grow_is_not_verified_and_leaves_the_typing_unknown(send, home):
    transcript = home / "t.jsonl"
    transcript.write_text("a\n")
    with mock.patch.object(send, "find_transcript_for_ws", return_value=str(transcript)), \
            mock.patch.object(send, "file_size", side_effect=lambda p: 2):
        code, record = run(send, Rpc(home), "--enter")
    assert code == 0 and record["transcript_size_delta"] == 0
    assert ledger(home)[1]["verified"] is False and ledger(home)[1]["outcome"] == "unknown"
    assert ledger(home)[1]["error"] == "not confirmed"


def test_keys_that_cant_be_made_after_the_intent_exit_4_and_close_it_as_failed(send, home):
    rpc = Rpc(home)
    with mock.patch.object(send.session_effects, "Keys",
                           side_effect=send.session_effects.Refused("invalid", "no keys")):
        code, record = run(send, rpc, "--enter")
    assert code == 4 and rpc.calls == [] and record["refused"]["reason"] == "no keys"
    assert ledger(home)[1]["outcome"] == "failed"


@pytest.mark.parametrize("answers, outcome", [([(124, {})], "rpc_send_text_timeout"),
                                              ([(0, {}), (124, {})], "rpc_send_key_timeout"),
                                              ([(1, {})], "rpc_send_text_failed"),
                                              ([(0, {}), (1, {})], "rpc_send_key_failed")])
def test_a_timeout_is_its_own_result_apart_from_a_refusal(send, home, answers, outcome):
    code, record = run(send, Rpc(home, answers), "--enter")
    assert record["outcome"] == outcome and code == (5 if outcome.endswith("timeout") else 2)


def test_the_pulse_takes_a_timeout_for_text_that_may_have_been_typed_not_for_text_not_sent(home):
    pulse = load_pulse(home)
    with mock.patch.object(pulse, "run", return_value=(5, json.dumps({"outcome": "rpc_send_text_timeout"}), "")):
        assert pulse.cmux_send("workspace:1000010003", "/cleanup")["outcome"] == "may-have-been-typed"
    with mock.patch.object(pulse, "cmux_send", return_value={"outcome": "may-have-been-typed"}), \
            mock.patch.object(pulse, "previous_send_ingested", return_value=True), \
            mock.patch.object(pulse, "observer_actions_held", return_value=False), \
            mock.patch.object(pulse, "pre_cleanup_check", return_value={"gate": "ok"}), \
            mock.patch.object(pulse, "assistant_merged_workspace", return_value=True):
        action = pulse.execute_verdict({"ref": "workspace:1000010003", "title": "t", "cwd": "/"},
                                       {"verdict": "ready_for_cleanup", "summary": "s", "next": "n"}, [])
    assert action["outcome"] == "failed" and "may have been typed" in action["evidence"]


def test_the_merge_script_takes_a_timeout_for_a_command_that_may_already_be_in_the_session(home):
    merge = load_module(home, "merge-pr-dispatch.py")
    with mock.patch.object(merge.subprocess, "run",
                           return_value=mock.Mock(returncode=5, stdout=json.dumps({"outcome": "rpc_send_text_timeout"}),
                                                  stderr="")):
        ok, why = merge.step2_send_and_verify("workspace:1000010002", "/merge-when-ready 7", "standing")
    assert ok is False and why.startswith("may_have_been_typed: cmux didn't answer in time")


@pytest.mark.parametrize("observed, title", [
    ("may_have_been_typed: cmux didn't answer in time", "PR #7: /merge-when-ready 7 may already be in workspace:1000010001"),
    ("something else", "PR #7: /merge-when-ready 7 did not submit into workspace:1000010001")])
def test_the_merge_card_says_the_command_may_already_be_there_when_cmux_didnt_answer(home, observed, title):
    merge = load_module(home, "merge-pr-dispatch.py")
    sys.argv = ["merge-pr-dispatch.py", "--ws", "workspace:1000010001", "--pr", "7"]
    out = io.StringIO()
    with mock.patch.object(merge, "step0_safety_gate", return_value=(True, "", {})), \
            mock.patch.object(merge, "gh_pr_view", return_value={"files": []}), \
            mock.patch.object(merge, "squirrel_e2e_gate", return_value=(True, "")), \
            mock.patch.object(merge, "step_freeze_retarget", return_value=(False, "", {})), \
            mock.patch.object(merge, "step1_ci_route", return_value=("merge-when-ready", "", {})), \
            mock.patch.object(merge, "step2_send_and_verify", return_value=(False, observed)), \
            mock.patch("sys.stdout", out), pytest.raises(SystemExit) as stop:
        merge.main()
    card = json.loads(out.getvalue())["awaiting_card"]
    assert stop.value.code == 2 and card["title"] == title
    assert "may_have_been_typed" not in card["detail"]  # the token is for the script's result, not for a person
    assert (card["detail"] == "cmux didn't answer in time") == observed.startswith("may_have")


def test_a_script_run_by_an_agent_with_its_defaults_is_limited_and_held_like_everything_else(send, home):
    """The default source and authorization (what agents are told to use) get no exemption."""
    for _ in range(3):
        assert run(send, Rpc(home), "--enter")[0] == 0
    rpc = Rpc(home)
    code, record = run(send, rpc, "--enter")  # the fourth identical run
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "loop"


def test_a_default_run_right_after_a_typing_nobody_could_tell_about_is_refused(send, home):
    run(send, Rpc(home, [(0, {}), (124, {})]), "--enter")  # the Enter timed out: unknown
    rpc = Rpc(home)
    code, record = run(send, rpc, "--enter")
    assert code == 4 and rpc.calls == [] and record["refused"]["code"] == "unknown"
    assert "type it into the session yourself" in record["refused"]["reason"]


def test_no_option_of_the_script_can_claim_the_one_time_exemption(send):
    import ast
    source = (Path(send.__file__).read_text())
    assert "once_tap" not in source
    options = {a.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"
               for a in n.args if isinstance(a, ast.Constant)}
    assert options == {"--ws", "--text", "--enter", "--caller", "--source", "--auth-kind", "--auth-ref", "--task",
                       "--no-log", "--post-send-wait"}
    for name in ("todo-server.py", "cmux-restore-sessions.py", "merge-pr-dispatch.py", "pulse.py"):
        assert "once_tap" not in (Path(send.__file__).parent / name).read_text(), name


@pytest.fixture(autouse=True)
def pro_mode(monkeypatch):
    """Everything this file tests is Pro mode's; the test says so, instead of leaning on the suite's default."""
    from assistant import mode
    monkeypatch.setattr(mode, "current", lambda: "pro")


def test_the_pro_send_logs_a_send_with_no_terminal_and_a_good_send_to_the_send_log(send, home):
    send.SENDS_LOG = str(home / "sends.jsonl")
    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", "hi", "--post-send-wait", "0"]
    with mock.patch.object(send, "resolve_terminal_surface", return_value=(None,) * 4), \
            mock.patch("sys.stdout", io.StringIO()), pytest.raises(SystemExit) as stop:
        send.main()
    assert stop.value.code == 1
    sys.argv = ["cmux-send.py", "--ws", "workspace:1000010005", "--text", "hi", "--post-send-wait", "0",
                "--source", "cleanup", "--auth-kind", "standing"]
    with mock.patch.object(send, "resolve_terminal_surface", return_value=SURFACE), \
            mock.patch.object(send, "rpc", Rpc(home)), mock.patch.object(send.time, "sleep"), \
            mock.patch("sys.stdout", io.StringIO()), pytest.raises(SystemExit) as stop:
        send.main()
    assert stop.value.code == 0
    rows = [json.loads(line)["outcome"] for line in (home / "sends.jsonl").read_text().splitlines()]
    assert rows == ["no_terminal_surface", "sent"]
