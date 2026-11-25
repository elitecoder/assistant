"""src/assistant/session_effects.py: typing into an agent session goes through the effects ledger."""
from __future__ import annotations

import json

import pytest

from assistant import effects, session_effects as se

T0 = 1_793_613_610.0
MARKER = "MARKER-do-not-record"
WS = "workspace:1000010005"   # cmux numbers its refs from 1000010000
WS2 = "workspace:1000010006"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(effects, "_now", c)
    monkeypatch.setattr(se, "_now", c)
    return c


@pytest.fixture
def home(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("HOME", str(tmp_path))
    effects._forget()
    return tmp_path


def raw(home) -> str:
    return (home / ".assistant/effects/effects.jsonl").read_text()


def rows(home) -> list:
    return [json.loads(line) for line in raw(home).splitlines()]


def nudge(session=WS, **kw):
    return se.begin(session, "nudge:stranded", kind="standing", ref="nudge:stranded", component="pulse", **kw)


def slack(ts="1793613610.000100", anchor=T0 - 5, session=WS):
    return se.begin(session, "slack-message", kind="tap", ref=f"slack:{ts}", component="comms", slack_ts=ts,
                    anchor=anchor)


def refusal(call, *args, **kw) -> se.Refused:
    with pytest.raises(se.Refused) as exc:
        call(*args, **kw)
    return exc.value


# --- what is recorded -------------------------------------------------------------------------------------------

def test_begin_records_the_intent_with_the_session_the_source_and_what_allowed_it(home):
    ef = nudge(trace="tr-0123456789abcdef")
    row = rows(home)[0]
    assert row["id"] == ef and row["class"] == "session.type"
    assert row["target"] == {"source": "nudge:stranded", "session": WS}
    assert row["auth"]["kind"] == "standing" and row["auth"]["ref"] == "nudge:stranded"
    assert row["trace"] == "tr-0123456789abcdef" and row["component"] == "pulse"


def test_a_task_a_slack_time_and_a_terminal_are_recorded_as_pointers(home):
    se.begin(WS, "task", kind="owner_flag", ref="td-12", component="pulse", task="td-12", trace="junk")
    se.begin("", "restore", kind="tap", ref="cli:restore", component="restore",
             surface="3F2504E0-4F89-41D3-9A0C-0305E82C3301")
    slack()
    got = [r["target"] for r in rows(home)]
    assert got[0] == {"source": "task", "session": WS, "task": "td-12"}
    assert rows(home)[0]["trace"] == ""  # a trace that isn't one is dropped, not recorded
    assert got[1] == {"source": "restore", "surface": "3F2504E0-4F89-41D3-9A0C-0305E82C3301"}
    assert got[2]["ts"] == "1793613610.000100"


@pytest.mark.parametrize("digits, accepted", [(1, True), (10, True), (18, True), (19, False)])
def test_a_session_ref_of_one_to_eighteen_digits_is_accepted(home, digits, accepted):
    ref = "workspace:" + "1" * digits
    if accepted:
        assert nudge(ref)
    else:
        assert refusal(nudge, ref).code == "invalid"


@pytest.mark.parametrize("session, source, surface", [
    ("workspace:abc", "task", ""), ("/etc/passwd", "task", ""), ("workspace:12/../x", "task", ""),
    ("not-a-session", "task", ""), ("", "task", ""), (WS, "Bad Source!", ""), ("", "restore", "not-a-uuid"),
])
def test_a_target_that_is_not_a_pointer_is_refused_and_nothing_is_written(home, session, source, surface):
    exc = refusal(se.begin, session, source, kind="tap", ref="x", component="c", surface=surface)
    assert exc.code == "invalid" and not (home / ".assistant/effects/effects.jsonl").exists()
    assert str(exc) == ("Assistant didn't recognise the session this was meant for (it expects workspace:N), so it "
                        "typed nothing.")


def test_an_unaccepted_kind_of_allowance_is_refused(home):
    assert refusal(se.begin, WS, "task", kind="decision", ref="td-1", component="c").code == "unauthorized"
    assert refusal(se.begin, WS, "task", kind="standing", ref="not a ref!", component="c").code == "unauthorized"


def test_an_intent_that_cant_be_written_is_refused_with_a_sentence_a_person_can_read(home, monkeypatch):
    def broken(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(effects, "_write", broken)
    exc = refusal(nudge)
    assert exc.code == "unrecorded" and "disk" in str(exc)


def test_a_malformed_value_from_the_ledger_is_refused(home, monkeypatch):
    monkeypatch.setattr(effects, "begin", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert refusal(nudge).code == "invalid"


def test_the_ledger_never_holds_the_text_typed_a_prompt_path_or_a_title(home):
    se.begin(WS, "task", kind="owner_flag", ref="td-3", component="pulse", task="td-3")
    se.finish(nudge(), "done", verified=True)
    assert MARKER not in raw(home)


# --- loops ------------------------------------------------------------------------------------------------------

def test_the_fourth_identical_typing_in_half_an_hour_is_refused_and_the_words_name_the_session(home, clock):
    for _ in range(3):
        se.finish(nudge(), "done")
        clock.t += 60
    exc = refusal(nudge)
    assert exc.code == "loop"
    assert str(exc) == (f"Assistant already typed this into {WS} three times in the last half hour, so it is waiting. "
                        "It tries again once the oldest of them is 30 minutes old.")
    # another source, another session, and the same thing once the oldest is half an hour old are not the loop
    se.begin(WS, "cleanup", kind="standing", ref="cleanup", component="pulse")
    nudge(WS2)
    clock.t = T0 + se.LOOP_SEC + 1
    nudge()


def test_a_typing_nobody_could_tell_about_and_one_still_open_count_toward_the_loop(home, clock):
    def go():
        return se.begin(WS, "trust", kind="standing", ref="slack-comms", component="comms")
    se.finish(go(), "unknown")
    clock.t += 5
    go()  # never gets a result
    clock.t += 700  # past the ten minutes in which the ledger calls it in progress
    se.finish(go(), "unknown")
    clock.t += 5
    assert refusal(go).code == "loop"


def test_the_loop_counts_typings_twenty_minutes_back_and_ignores_older_ones(home, clock):
    for _ in range(3):
        se.finish(nudge(), "done")
        clock.t += 400
    clock.t = T0 + 1500  # the first was 1500 s ago: inside half an hour, so all three count
    assert refusal(nudge).code == "loop"
    clock.t = T0 + 1801  # the oldest has aged out
    nudge()


def test_an_old_row_in_the_middle_of_the_ledger_does_not_end_the_count(home, clock):
    clock.t = T0 + 3000
    for _ in range(3):
        se.finish(nudge(), "done")
        clock.t += 1
    clock.t = T0 + 100  # a clock that went back wrote a row that looks old, last
    se.finish(nudge(WS2), "failed")
    clock.t = T0 + 3100
    assert refusal(nudge).code == "loop"


def test_a_typing_that_failed_does_not_count_toward_the_loop(home, clock):
    for _ in range(5):
        se.finish(nudge(), "failed")
        clock.t += 1
    nudge()


def test_each_task_is_its_own_thing_for_the_loop_limit(home, clock):
    for n in range(5):
        se.finish(se.begin(WS, "task", kind="owner_flag", ref=f"td-{n}", component="pulse", task=f"td-{n}"), "done")
        clock.t += 1


def test_a_typing_that_finished_a_moment_ago_does_not_block_the_next_one_in_the_same_second(home, clock):
    clock.t = T0 + 0.6
    se.finish(nudge(), "done")
    clock.t += 0.1
    nudge()  # its allowance is "now", rounded up, so it can't fall before the result just written


# --- unknown typings ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["nudge:stranded", "cleanup", "boot", "clear", "merge", "cli", "restore", "run-now"])
@pytest.mark.parametrize("kind", ["standing", "tap"])
def test_an_unknown_typing_holds_the_same_source_in_the_same_session_for_half_an_hour_only(home, clock, source, kind):
    """Whatever allowed it: a script run by hand or by an agent, "run now" and restore are held like the pulse's."""
    def go(session=WS):
        return se.begin(session, source, kind=kind, ref="x:y", component="pulse")
    se.finish(go(), "unknown", error="timeout")
    clock.t += 600
    exc = refusal(go)
    assert exc.code == "unknown"
    assert str(exc) == (f"Assistant typed this into {WS} earlier and couldn't tell if it arrived, so it won't type it "
                        f"again by itself. Look at that session. If it isn't there, type it into the session yourself.")
    assert "cmux-send.py" not in str(exc)
    go(WS2)  # another session is its own target
    clock.t += se.LOOP_SEC
    go()     # half an hour on, it may be tried again, and a reused session ref isn't blocked for ever


def test_a_typing_that_never_got_a_result_is_held_once_its_window_has_passed_and_for_half_an_hour(home, clock):
    nudge()  # the process stopped before it could say how it went
    clock.t += effects.IN_FLIGHT_SEC - 1
    exc = refusal(nudge)
    assert exc.code == "in-progress"
    assert str(exc) == f"Assistant may still be typing this into {WS} (or stopped partway), so it waited."
    clock.t += 2
    assert refusal(nudge).code == "unknown"
    clock.t = T0 + se.LOOP_SEC + 1
    nudge()


def test_a_task_keeps_the_ledgers_own_rule_and_a_slack_message_is_not_typed_again_without_a_newer_allowance(home, clock):
    first = slack(anchor=T0 - 5)
    assert refusal(slack, anchor=T0 - 5).code == "in-progress"
    se.finish(first, "unknown")
    clock.t += 7200  # however long it has been
    assert refusal(slack, anchor=T0 - 5).code == "unknown"
    se.finish(slack(anchor=clock.t - 1), "done")  # the owner sent it again: a newer allowance
    assert refusal(slack, anchor=clock.t - 2).code == "done"


def test_a_message_dated_after_this_macs_clock_waits_and_is_not_dropped(home):
    exc = refusal(slack, anchor=T0 + 3600)
    assert exc.code == "later" and "clock" in str(exc)


def test_the_remaining_sentences(home):
    assert str(se.Refused("done", se.DONE.format(where=WS))) == f"Assistant already typed this into {WS}, so it didn't type it again."
    assert se.UNAUTHORIZED == ("Assistant couldn't confirm what allowed this, so it typed nothing. Run it again from "
                               "the place you meant.")


# --- Keys --------------------------------------------------------------------------------------------------------

class Rpc:
    def __init__(self, refuse=(), boom=()):
        self.calls, self.refuse, self.boom = [], refuse, boom

    def __call__(self, method, params):
        self.calls.append((method, params))
        if len(self.calls) in self.boom:
            raise TimeoutError("cmux")
        return None if len(self.calls) in self.refuse else {}


def test_text_and_enter_both_sent_is_done_and_the_calls_are_the_two_keystrokes(home):
    rpc = Rpc()
    assert se.type_line(nudge(), rpc, "surface:2", "hi", session=WS, pause=1, sleep=lambda s: None) == "done"
    assert rpc.calls == [("surface.send_text", {"surface_id": "surface:2", "text": "hi"}),
                         ("surface.send_key", {"surface_id": "surface:2", "key": "enter"})]
    assert rows(home)[1]["outcome"] == "done"


def test_the_pause_between_text_and_enter_uses_the_clock_as_it_is_when_it_is_called(home, monkeypatch):
    slept = []
    monkeypatch.setattr(se.time, "sleep", slept.append)
    se.type_line(nudge(), Rpc(), "s", "hi", session=WS, pause=1)
    assert slept == [1]


def test_enter_as_a_character_and_no_enter_are_supported(home):
    rpc = Rpc()
    assert se.type_line(nudge(), rpc, "s", "hi", session=WS, enter="cr") == "done"
    assert rpc.calls[1] == ("surface.send_text", {"surface_id": "s", "text": "\r"})
    rpc = Rpc()
    assert se.type_line(nudge(WS2), rpc, "s", "1", session=WS2, enter="") == "done" and len(rpc.calls) == 1


def test_text_refused_is_failed_and_enter_is_not_pressed(home):
    rpc = Rpc(refuse=(1,))
    assert se.type_line(nudge(), rpc, "s", "hi", session=WS) == "failed" and len(rpc.calls) == 1
    assert rows(home)[1]["outcome"] == "failed" and rows(home)[1]["error"] == "tool failed"


def test_text_sent_and_enter_refused_is_unknown(home):
    assert se.type_line(nudge(), Rpc(refuse=(2,)), "s", "hi", session=WS) == "unknown"


def test_a_transport_error_is_unknown_whenever_it_happens_and_is_recorded_as_a_timeout(home):
    assert se.type_line(nudge(), Rpc(boom=(2,)), "s", "hi", session=WS) == "unknown"
    assert se.type_line(nudge(WS2), Rpc(boom=(1,)), "s", "hi", session=WS2) == "unknown"
    assert rows(home)[-1]["error"] == "timeout"


def test_a_typing_nothing_could_confirm_is_unknown_and_one_seen_to_land_is_done(home):
    keys = se.Keys(nudge(), Rpc(), "s", WS)
    assert keys.outcome() == "failed"
    keys.text("a")
    keys.key("enter")
    assert keys.outcome() == "done"
    keys.confirmed = False  # the transcript was checked and the text wasn't there
    assert keys.outcome() == "unknown"
    keys.confirmed = True
    keys.finish()
    keys.finish()  # once
    assert [r["kind"] for r in rows(home)] == ["intent", "result"] and rows(home)[1]["verified"] is True


def test_a_typing_with_one_keystroke_sent_and_one_refused_is_unknown(home):
    keys = se.Keys(nudge(), Rpc(refuse=(1,)), "s", WS)
    assert not keys.carriage_return() and keys.outcome() == "failed"
    keys.text("a")
    assert keys.outcome() == "unknown"


def test_unconfirmed_results_carry_their_reason(home):
    keys = se.Keys(nudge(), Rpc(), "s", WS)
    keys.text("a")
    keys.confirmed = False
    keys.finish()
    assert rows(home)[1]["outcome"] == "unknown" and rows(home)[1]["error"] == "not confirmed"


def test_nothing_can_be_typed_without_an_intent_on_disk_that_is_still_open(home):
    rpc = Rpc()
    for bad in ("ef-0123456789abcdef", None):
        assert refusal(se.Keys, bad, rpc, "s", WS).code == "unrecorded"
    ef = nudge()
    se.finish(ef, "done")
    assert refusal(se.type_line, ef, rpc, "s", "again", session=WS).code == "unrecorded"  # its result is in
    assert rpc.calls == []


def test_an_intent_is_found_by_its_id_however_much_was_written_after_it(home, clock, monkeypatch):
    monkeypatch.setattr(se, "LOOP_SCAN", 5)  # so a handful of later effects is "more than the newest few"
    ef = nudge()
    for n in range(12):
        se.finish(nudge(f"workspace:{1000020000 + n}"), "failed")
    rpc = Rpc()
    assert se.type_line(ef, rpc, "s", "hi", session=WS) == "done" and len(rpc.calls) == 2


def test_keys_are_for_the_session_or_terminal_the_intent_names_and_one_for_each_effect(home):
    ef = nudge()
    assert refusal(se.Keys, ef, Rpc(), "s", WS2).code == "invalid"   # another session
    assert refusal(se.Keys, ef, Rpc(), "s", "").code == "invalid"    # none
    keys = se.Keys(ef, Rpc(), "s", WS)
    assert refusal(se.Keys, ef, Rpc(), "s", WS).code == "unrecorded"  # a second one for the same effect
    keys.finish()
    uuid = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"
    by_terminal = se.begin("", "restore", kind="tap", ref="cli:restore", component="r", surface=uuid)
    assert refusal(se.Keys, by_terminal, Rpc(), "3F2504E0-4F89-41D3-9A0C-0305E82C3302").code == "invalid"
    se.Keys(by_terminal, Rpc(), uuid)


def test_a_keystroke_after_the_intent_was_finished_elsewhere_is_refused(home):
    rpc = Rpc()
    ef = nudge()
    keys = se.Keys(ef, rpc, "s", WS)
    keys.text("a")
    se.finish(ef, "failed")  # closed by somebody else
    with pytest.raises(se.Refused):
        keys.key("enter")
    assert len(rpc.calls) == 1


def test_a_typing_whose_keys_cant_be_made_is_closed_as_failed(home):
    ef = nudge()
    with pytest.raises(se.Refused):
        se.type_line(ef, Rpc(), "s", "hi", session=WS2)
    assert rows(home)[1]["outcome"] == "failed"


# --- the ledger's write test and small helpers ---------------------------------------------------------------------

def test_writable_proves_a_real_write_and_leaves_nothing_behind(home, monkeypatch):
    assert se.writable()
    assert list((home / ".assistant/effects").glob(".probe-*")) == []
    monkeypatch.setattr(se.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert not se.writable()


def test_writable_is_false_when_the_folder_cant_be_made_or_the_write_is_refused(home, monkeypatch):
    monkeypatch.setattr(se.os, "write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    assert not se.writable()
    monkeypatch.setattr(se.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert not se.writable()


def test_trace_of_is_the_tasks_own_or_its_stand_in_and_nothing_without_a_task():
    from assistant import todostore
    assert se.trace_of({"id": "td-1", "trace": "tr-0123456789abcdef"}) == "tr-0123456789abcdef"
    item = {"id": "td-1", "trace": "nope", "createdAt": "2026-11-01T00:00:00Z"}
    assert se.trace_of(item) == todostore.trace_of(item) and se.trace_of(item).startswith("tl-")
    assert se.trace_of(None) == ""


def test_the_ledger_finds_an_effect_by_its_id_and_says_nothing_for_another(home):
    ef = nudge()
    found = effects.get(ef)
    assert found["id"] == ef and found["outcome"] is None and found["target"]["session"] == WS
    se.finish(ef, "done")
    assert effects.get(ef)["outcome"] == "done"
    assert effects.get("ef-0000000000000000") is None and effects.get(None) is None


def test_line_counts_an_unexpected_transport_error_and_does_not_raise_it(home):
    class Odd:
        def __call__(self, method, params):
            raise RuntimeError("a bug in the transport")
    ef = nudge()
    keys = se.Keys(ef, Odd(), "s", WS)
    keys.line("hi")
    keys.finish()
    assert rows(home)[1]["outcome"] == "unknown"


def test_line_lets_a_refusal_because_the_intent_was_closed_go_up(home):
    ef = nudge()
    keys = se.Keys(ef, Rpc(), "s", WS)
    se.finish(ef, "failed")
    with pytest.raises(se.Refused):
        keys.line("hi")


@pytest.mark.parametrize("source", ["merge", "cli", "restore", "run-now", "nudge:stranded"])
@pytest.mark.parametrize("kind", ["tap", "standing", "owner_flag"])
def test_every_typing_is_limited_to_three_in_half_an_hour_whatever_allowed_it(home, clock, source, kind):
    def go():
        return se.begin(WS, source, kind=kind, ref="x:y", component="c")
    for _ in range(se.LOOP_MAX):
        se.finish(go(), "done")
        clock.t += 5
    assert refusal(go).code == "loop"  # the fourth identical one, run by a script, an agent or a person's button


def test_a_hand_run_merge_script_is_held_after_an_unknown_like_the_pulses(home, clock):
    def merge(kind):
        return se.begin(WS, "merge", kind=kind, ref="merge", component="merge-pr-dispatch.py")
    se.finish(merge("standing"), "unknown")
    clock.t += 60
    assert refusal(merge, "standing").code == "unknown"
    assert refusal(merge, "tap").code == "unknown"  # run by hand: a script can't tell a person from an agent


def test_an_owners_slack_messages_are_typed_at_once_four_in_a_row_whatever_came_before(home, clock):
    se.finish(se.begin(WS, "nudge:stranded", kind="standing", ref="nudge:stranded", component="pulse"), "unknown")
    for n in range(se.LOOP_MAX + 1):
        ts = f"17936136{10 + n}.000100"
        ef = se.begin(WS, "slack-message", kind="tap", ref=f"slack:{ts}", component="comms", slack_ts=ts,
                      anchor=clock.t - 5, once_tap=f"slack:{ts}")
        se.finish(ef, "unknown")  # each is the first use of its own one-time tap
        clock.t += 1


def test_a_tap_is_exempt_only_the_first_time_it_is_used_in_a_session(home, clock):
    def retry(session=WS, tap="slack:1793613610.000100"):
        return se.begin(session, "submit", kind="tap", ref=tap, component="comms", slack_ts="1793613610.000100",
                        once_tap=tap)
    first = se.begin(WS, "slack-message", kind="tap", ref="slack:1793613610.000100", component="comms",
                     slack_ts="1793613610.000100", anchor=T0 - 5, once_tap="slack:1793613610.000100")
    se.finish(first, "unknown")
    for _ in range(se.LOOP_MAX):  # the same tap again: counted, so it stops at the loop limit
        se.finish(retry(), "unknown")
        clock.t += 5
    assert refusal(retry).code == "loop"
    se.finish(retry(WS2), "done")  # another session has not used it yet
    se.finish(se.begin(WS, "submit", kind="tap", ref="slack:1793613611.000100", component="comms",
                       slack_ts="1793613611.000100", once_tap="slack:1793613611.000100"), "done")  # a new tap, at once


@pytest.fixture(autouse=True)
def pro_mode(monkeypatch):
    """Everything this file tests is Pro mode's; the test says so, instead of leaning on the suite's default."""
    from assistant import mode
    monkeypatch.setattr(mode, "current", lambda: "pro")
