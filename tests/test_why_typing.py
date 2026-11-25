"""The trail of a row that was sent to an agent, read through the real stores after the pulse's own dispatch (with cmux
faked, as tests/test_session_effects_pulse.py does it): a start is one step that says what was typed."""
from __future__ import annotations

import json

from assistant import effects, telemetry, why

from test_session_effects_pulse import Cmux, MARKER, READY, TRUST, WS, home, mod, seed  # noqa: F401 - the fixtures

TRACE = "tr-0123456789abcdef"


def trail(read) -> list:
    return [s["text"] for s in read["steps"]]


def say_sent(mode="assist"):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="open", origin="work-queue", mode=mode)


def test_a_start_that_answered_trust_and_typed_the_instruction_reads_as_one_step(mod, home, capsys):
    seed(mod, detail=MARKER + " Slack: xoxb-NOT-A-REAL-TOKEN")
    say_sent()
    cmux = Cmux(mod, home, screens=(TRUST, READY))
    assert cmux.dispatch() is True
    read = why.read(TRACE)
    story = trail(read)
    print("\nTRAIL (trust answered, instruction typed, nothing seen arrive):\n  " + "\n  ".join(story))
    assert story == ["You sent it to an agent for a first pass. It became task td-1.",
                     "Assistant started an agent on it and typed the task's instruction, but couldn't tell if it arrived."]
    assert read["state"] == "ok" and read["footer"].startswith("This is everything")
    assert not [t for t in story if "setting you turned on" in t or "Typed into" in t]
    raw = json.dumps(read)
    assert MARKER not in raw and "xoxb" not in raw and "workspace:" not in raw and "spawn-prompts" not in raw


def test_the_ledger_holds_two_typings_for_that_start_and_both_are_paired_with_it(mod, home):
    seed(mod)
    say_sent()
    assert Cmux(mod, home, screens=(TRUST, READY)).dispatch() is True
    typed = [e for e in effects.history(trace=TRACE) if e["class"] == "session.type"]
    assert sorted(e["target"]["source"] for e in typed) == ["task", "trust"]
    assert len(trail(why.read(TRACE))) == 2


def test_a_start_with_only_the_instruction_reads_the_same(mod, home):
    seed(mod)
    say_sent("handsoff")
    assert Cmux(mod, home).dispatch() is True
    assert trail(why.read(TRACE)) == ["You sent it to an agent, hands-off. It became task td-1.",
                                      "Assistant started an agent on it and typed the task's instruction, but couldn't tell if it arrived."]


def test_a_start_cmux_refused_says_its_own_sentence_once_and_the_ledgers_failed_typing_adds_nothing(mod, home):
    seed(mod)
    say_sent()
    assert Cmux(mod, home, refuse=("surface.send_text",)).dispatch() is False
    story = trail(why.read(TRACE))
    print("\nTRAIL (cmux refused the instruction):\n  " + "\n  ".join(story))
    assert story == ["You sent it to an agent for a first pass. It became task td-1.",
                     "cmux refused the instruction, so nothing was typed. The task is parked."]


def test_two_starts_of_one_task_each_keep_their_own_typings(mod, home):
    seed(mod)
    say_sent()
    assert Cmux(mod, home, refuse=("surface.send_text",)).dispatch() is False
    items = json.loads(mod.TODO_PATH.read_text())
    items["items"][0].update(status="open", parkedBy=None)
    mod.TODO_PATH.write_text(json.dumps(items))
    mod._dispatch_started.clear()
    assert Cmux(mod, home, workspace="workspace:1000010008").dispatch() is True
    story = trail(why.read(TRACE))
    print("\nTRAIL (a refused start, then a good one):\n  " + "\n  ".join(story))
    assert story[1:] == ["cmux refused the instruction, so nothing was typed. The task is parked.",
                         "Assistant started an agent on it and typed the task's instruction, but couldn't tell if it arrived."]
