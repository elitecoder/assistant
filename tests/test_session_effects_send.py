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
    sys.argv = ["cmux-send.py", "--ws", "workspace:5", "--text", text, "--no-log", "--post-send-wait", "0", *argv]
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
    assert rows[0]["target"] == {"source": "cleanup", "session": "workspace:5"}
    assert rows[0]["auth"]["kind"] == "standing" and rows[0]["auth"]["ref"] == "cleanup"
    assert rows[0]["component"] == "assistant-pulse"
    assert rows[1]["outcome"] == "done"
    assert MARKER not in (home / ".assistant/effects/effects.jsonl").read_text()


def test_run_by_hand_it_counts_as_a_persons_tap_and_the_task_is_recorded(send, home):
    code, _ = run(send, Rpc(home), "--task", "td-9")
    row = ledger(home)[0]
    assert code == 0 and row["auth"]["kind"] == "tap" and row["auth"]["ref"] == "cli:cmux-send"
    assert row["target"] == {"source": "cli", "session": "workspace:5", "task": "td-9"}


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
    ([(124, {"error": "timeout"})], ["--enter"], 2, "unknown"),              # can't tell
    ([(0, {}), (124, {"error": "timeout"})], ["--enter"], 2, "unknown"),
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
    sys.argv = ["cmux-send.py", "--ws", "workspace:5", "--text", "hi", "--no-log"]
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
        pulse.cmux_send("workspace:3", text)
    cmd = ran.call_args.args[0]
    flags = flags_of(cmd)
    assert flags["--source"] == source and flags["--auth-kind"] == "standing" and flags["--auth-ref"] == source


def test_a_refused_typing_is_a_send_that_failed_for_the_pulse(home):
    pulse = load_pulse(home)
    with mock.patch.object(pulse, "run", return_value=(4, "{}", "cmux-send: Assistant couldn't record")):
        assert pulse.cmux_send("workspace:3", "/cleanup") == {"outcome": "failed", "transcript_size_delta": 0}


def test_merge_dispatch_says_who_allowed_it_and_reports_a_refusal(home):
    merge = load_module(home, "merge-pr-dispatch.py")
    refused = json.dumps({"outcome": "refused", "refused": {"reason": "Assistant couldn't record it"}})
    with mock.patch.object(merge.subprocess, "run",
                           return_value=mock.Mock(returncode=4, stdout=refused, stderr="")) as ran:
        ok, why = merge.step2_send_and_verify("workspace:2", "/merge-when-ready 7", "standing")
    cmd = ran.call_args.args[0]
    flags = flags_of(cmd)
    assert (ok, flags["--source"], flags["--auth-kind"], flags["--auth-ref"]) == (False, "merge", "standing", "merge")
    assert "couldn't record it" in why
    with mock.patch.object(merge.subprocess, "run", return_value=mock.Mock(returncode=4, stdout=refused, stderr="")) as ran:
        merge.step2_send_and_verify("workspace:2", "/merge-when-ready 7")
    assert "tap" in ran.call_args.args[0]


def test_the_pulse_runs_merge_dispatch_as_standing_and_a_person_as_a_tap(home):
    pulse = load_pulse(home)
    with mock.patch.object(pulse.subprocess, "run",
                           return_value=mock.Mock(returncode=0, stdout=json.dumps({"outcome": "submitted"}))) as ran:
        pulse.run_merge_pr_dispatch("workspace:1", 7, False, False)
    cmd = ran.call_args.args[0]
    assert cmd[cmd.index("--authorized-by") + 1] == "standing"
    merge = load_module(home, "merge-pr-dispatch.py")
    sys.argv = ["merge-pr-dispatch.py", "--ws", "workspace:1", "--pr", "7"]
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
