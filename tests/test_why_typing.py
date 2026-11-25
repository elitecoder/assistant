"""The trail of a row that was sent to an agent, read through the real stores after the pulse's own dispatch (with cmux
faked, as tests/test_session_effects_pulse.py does it): a start is one step that says what was typed."""
from __future__ import annotations

import json
from unittest import mock

from assistant import effects, session_effects, telemetry, why

from test_session_effects_pulse import Cmux, MARKER, READY, TRUST, WS, home, mod, seed  # noqa: F401 - the fixtures

from test_todo_server import mod as todo_server  # noqa: E402

TRACE = "tr-0123456789abcdef"
REFUSED = "cmux refused the instruction, so nothing was typed. "
BLOCKED = ('The task is marked blocked. To run it again, close the workspace it opened and press "Queue to start" '
           "on task td-1 on the Tasks tab of the Assistant dashboard.")


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
                     "Assistant started an agent on it and typed the task's instruction, but couldn't tell if the session received it. Look at the session to check."]
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
                                      "Assistant started an agent on it and typed the task's instruction, but couldn't tell if the session received it. Look at the session to check."]


def test_a_start_cmux_refused_says_its_own_sentence_once_and_the_ledgers_failed_typing_adds_nothing(mod, home):
    seed(mod)
    say_sent()
    assert Cmux(mod, home, refuse=("surface.send_text",)).dispatch() is False
    story = trail(why.read(TRACE))
    print("\nTRAIL (cmux refused the instruction):\n  " + "\n  ".join(story))
    assert story == ["You sent it to an agent for a first pass. It became task td-1.",
                     REFUSED + BLOCKED]


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
    assert story[1:] == [REFUSED + BLOCKED,
                         "Assistant started an agent on it and typed the task's instruction, but couldn't tell if the session received it. Look at the session to check."]


def test_a_session_whose_typing_was_refused_is_blocked_and_queue_to_start_really_opens_it_again(mod, home, monkeypatch):
    seed(mod)
    say_sent()
    cmux = Cmux(mod, home, screens=(TRUST,))
    with mock.patch.object(session_effects, "Keys", side_effect=session_effects.Refused("unrecorded", "x")):
        assert cmux.dispatch() is False
    task = json.loads(mod.TODO_PATH.read_text())["items"][0]
    assert task["status"] == "blocked" and task["parkedBy"] == "typing_refused"
    story = trail(why.read(TRACE))
    print("\nTRAIL (a session opened, then Assistant wouldn't type into it):\n  " + "\n  ".join(story))
    assert story == ["You sent it to an agent for a first pass. It became task td-1.",
                     "Assistant opened a session but wouldn't type into it, because it couldn't record what it types. " + BLOCKED]
    monkeypatch.setattr(todo_server, "JSON_PATH", mod.TODO_PATH)
    monkeypatch.setattr(todo_server, "rerender", lambda: None)
    assert todo_server.dispatch_now("td-1")[0] is True  # what pressing "Queue to start" does
    assert json.loads(mod.TODO_PATH.read_text())["items"][0]["status"] == "open"


def test_a_start_refused_before_any_session_was_opened_leaves_the_task_open_and_says_it_will_try_again(mod, home):
    seed(mod)
    say_sent()
    cmux = Cmux(mod, home)
    with mock.patch.object(session_effects, "writable", return_value=False):
        assert cmux.dispatch() is False
    assert cmux.calls == [] and stamped_status(mod) == "open"
    story = trail(why.read(TRACE))
    print("\nTRAIL (Assistant couldn't record what it types, before opening any session):\n  " + "\n  ".join(story))
    assert story == ["You sent it to an agent for a first pass. It became task td-1.",
                     "Assistant couldn't record what it types, so it didn't start an agent. It will try again by itself."]


def stamped_status(mod) -> str:
    return json.loads(mod.TODO_PATH.read_text())["items"][0]["status"]


def test_a_start_that_failed_after_the_instruction_was_typed_still_shows_the_typing(mod, home):
    seed(mod)
    say_sent()
    cmux = Cmux(mod, home, screens=(TRUST, READY))
    with mock.patch.object(mod, "_mark_todo_dispatched", return_value=False):
        assert cmux.dispatch() is False
    story = trail(why.read(TRACE))
    print("\nTRAIL (the agent was started but the task couldn't be marked):\n  " + "\n  ".join(story))
    assert story[0].startswith("You sent it")
    assert "Assistant answered the new session's folder-trust question." in story
    assert "Assistant started an agent on it, but couldn't mark the task as started, so it may start a second one by itself." in story
    assert any("typed the task's instruction" in line for line in story)
