"""src/assistant/session_effects.py: typing into an agent session goes through the effects ledger."""
from __future__ import annotations

import json

import pytest

from assistant import effects, session_effects as se

T0 = 1_793_613_610.0
MARKER = "MARKER-do-not-record"


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


def nudge(session="workspace:4", **kw):
    return se.begin(session, "nudge:stranded", kind="standing", ref="nudge:stranded", component="pulse", **kw)


def test_begin_records_the_intent_with_the_session_the_source_and_what_allowed_it(home):
    ef = nudge(trace="tr-0123456789abcdef")
    row = rows(home)[0]
    assert row["id"] == ef and row["class"] == "session.type"
    assert row["target"] == {"source": "nudge:stranded", "session": "workspace:4"}
    assert row["auth"]["kind"] == "standing" and row["auth"]["ref"] == "nudge:stranded"
    assert row["trace"] == "tr-0123456789abcdef" and row["component"] == "pulse"


def test_a_task_a_slack_time_and_a_terminal_are_recorded_as_pointers(home):
    se.begin("workspace:7", "task", kind="owner_flag", ref="td-12", component="pulse", task="td-12", trace="junk")
    se.begin("", "restore", kind="tap", ref="cli:restore", component="restore",
             surface="3F2504E0-4F89-41D3-9A0C-0305E82C3301")
    se.begin("workspace:7", "slack-message", kind="tap", ref="slack:1793613610.000100", component="comms",
             slack_ts="1793613610.000100", anchor=T0)
    got = [r["target"] for r in rows(home)]
    assert got[0] == {"source": "task", "session": "workspace:7", "task": "td-12"}
    assert rows(home)[0]["trace"] == ""  # a trace that isn't one is dropped, not recorded
    assert got[1] == {"source": "restore", "surface": "3F2504E0-4F89-41D3-9A0C-0305E82C3301"}
    assert got[2]["ts"] == "1793613610.000100"


@pytest.mark.parametrize("session,source,surface", [
    ("not-a-session", "task", ""), ("", "task", ""), ("workspace:1", "Bad Source!", ""),
    ("", "restore", "not-a-uuid"),
])
def test_a_target_that_is_not_a_pointer_is_refused_and_nothing_is_written(home, session, source, surface):
    with pytest.raises(se.Refused) as exc:
        se.begin(session, source, kind="tap", ref="x", component="c", surface=surface)
    assert exc.value.code == "invalid" and not (home / ".assistant/effects/effects.jsonl").exists()


def test_an_unaccepted_kind_of_allowance_is_refused(home):
    with pytest.raises(se.Refused) as exc:
        se.begin("workspace:4", "task", kind="decision", ref="td-1", component="c")
    assert exc.value.code == "unauthorized"
    with pytest.raises(se.Refused) as exc:
        se.begin("workspace:4", "task", kind="standing", ref="not a ref!", component="c")
    assert exc.value.code == "unauthorized"


def test_an_intent_that_cant_be_written_is_refused_with_a_sentence_a_person_can_read(home, monkeypatch):
    def broken(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(effects, "_write", broken)
    with pytest.raises(se.Refused) as exc:
        nudge()
    assert exc.value.code == "unrecorded" and "disk" in str(exc.value)


def test_the_fourth_identical_typing_in_half_an_hour_is_refused(home, clock):
    for _ in range(3):
        se.finish(nudge(), "done")
        clock.t += 60
    with pytest.raises(se.Refused) as exc:
        nudge()
    assert exc.value.code == "loop"
    # another source, another session, and the same thing half an hour later are not the loop
    se.begin("workspace:4", "cleanup", kind="standing", ref="cleanup", component="pulse")
    nudge("workspace:5")
    clock.t += se.LOOP_SEC
    nudge()


def test_a_typing_that_failed_does_not_count_toward_the_loop(home, clock):
    for _ in range(5):
        se.finish(nudge(), "failed")
        clock.t += 1
    nudge()


def test_each_task_and_each_slack_message_is_its_own_thing(home, clock):
    for n in range(5):
        ts = f"1793613{610 + n}.000100"
        se.finish(se.begin("workspace:3", "slack-message", kind="tap", ref="slack:" + ts, component="comms",
                           slack_ts=ts, anchor=clock.t), "done")


def test_a_typing_that_may_have_landed_is_not_repeated_by_something_with_no_time_of_its_own(home, clock):
    se.finish(nudge(), "unknown", error="not confirmed")
    clock.t += 3600
    with pytest.raises(se.Refused) as exc:
        nudge()
    assert exc.value.code == "unknown"
    nudge("workspace:5")  # another session is its own target


def test_a_slack_message_that_may_have_landed_is_refused_by_the_ledgers_own_rule(home, clock):
    def go():
        return se.begin("workspace:3", "slack-message", kind="tap", ref="slack:1793613610.000100",
                        component="comms", slack_ts="1793613610.000100", anchor=1793613600.0)
    se.finish(go(), "unknown")
    with pytest.raises(se.Refused) as exc:
        go()
    assert exc.value.code == "unknown"


def test_a_done_slack_message_and_one_in_progress_are_refused(home):
    def go(anchor):
        return se.begin("workspace:3", "slack-message", kind="tap", ref="slack:1793613610.000100",
                        component="comms", slack_ts="1793613610.000100", anchor=anchor)
    first = go(T0 - 5)
    with pytest.raises(se.Refused) as exc:
        go(T0 - 5)
    assert exc.value.code == "in-progress"
    se.finish(first, "done")
    with pytest.raises(se.Refused) as exc:
        go(T0 - 5)
    assert exc.value.code == "done"


def test_a_malformed_tail_value_is_refused(home, monkeypatch):
    monkeypatch.setattr(effects, "begin", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    with pytest.raises(se.Refused) as exc:
        nudge()
    assert exc.value.code == "invalid"


def test_the_ledger_never_holds_the_text_typed_a_prompt_path_or_a_title(home):
    se.begin("workspace:4", "task", kind="owner_flag", ref="td-3", component="pulse", task="td-3")
    ef = nudge()
    se.finish(ef, "done", verified=True)
    assert MARKER not in raw(home)


def test_writable_says_whether_the_ledger_can_be_written(home, monkeypatch):
    assert se.writable()
    monkeypatch.setattr(se.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert not se.writable()


def test_trace_of_takes_only_a_real_trace_id():
    assert se.trace_of({"trace": "tr-0123456789abcdef"}) == "tr-0123456789abcdef"
    assert se.trace_of({"trace": "nope"}) == "" and se.trace_of(None) == ""


class Rpc:
    def __init__(self, refuse=(), boom=()):
        self.calls, self.refuse, self.boom = [], refuse, boom

    def __call__(self, method, params):
        self.calls.append((method, params))
        if len(self.calls) in self.boom:
            raise TimeoutError("cmux")
        return None if len(self.calls) in self.refuse else {}


def test_text_and_enter_both_sent_is_done_and_the_calls_are_the_two_keystrokes():
    rpc = Rpc()
    assert se.type_line(rpc, "surface:2", "hi", pause=1, sleep=lambda s: None) == "done"
    assert rpc.calls == [("surface.send_text", {"surface_id": "surface:2", "text": "hi"}),
                         ("surface.send_key", {"surface_id": "surface:2", "key": "enter"})]


def test_enter_as_a_character_and_no_enter_are_supported():
    rpc = Rpc()
    assert se.type_line(rpc, "s", "hi", enter="cr") == "done"
    assert rpc.calls[1] == ("surface.send_text", {"surface_id": "s", "text": "\r"})
    rpc = Rpc()
    assert se.type_line(rpc, "s", "1", enter="") == "done" and len(rpc.calls) == 1


def test_text_refused_is_failed_and_enter_is_not_pressed():
    rpc = Rpc(refuse=(1,))
    assert se.type_line(rpc, "s", "hi") == "failed" and len(rpc.calls) == 1


def test_text_sent_and_enter_refused_is_unknown():
    assert se.type_line(Rpc(refuse=(2,)), "s", "hi") == "unknown"


def test_a_transport_error_after_it_started_is_unknown_and_before_any_keystroke_is_failed():
    assert se.type_line(Rpc(boom=(2,)), "s", "hi") == "unknown"
    assert se.type_line(Rpc(boom=(1,)), "s", "hi") == "unknown"


def test_keys_report_confirmed_and_nothing_sent():
    keys = se.Keys(Rpc(), "s")
    assert keys.outcome() == "failed" and keys.outcome(confirmed=True) == "done"
    assert keys.text("a") and keys.key("enter") and keys.outcome() == "done"
    keys.rpc = Rpc(refuse=(1,))
    assert not keys.carriage_return() and keys.outcome() == "unknown"
