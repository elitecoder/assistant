"""Pro mode, in the ledger: a typing is held and counted by what is typed (for the sources whose text a person or an outside script
chooses), a task id the record can't hold doesn't stop a typing, and "run now" types what a proposal says."""
from __future__ import annotations

import json
import re
from unittest import mock

import pytest

from assistant import effects, feedback, session_effects as se

from test_session_effects import T0, WS, WS2, clock, home, refusal, rows  # noqa: F401 - fixtures

HEX16 = re.compile(r"[0-9a-f]{16}")
DIGEST_A, DIGEST_B = "0123456789abcdef", "fedcba9876543210"
SECRET_TEXT = "TEXT-THAT-MUST-NEVER-REACH-THE-LEDGER"


# --- 1. a task id the record can't hold ----------------------------------------------------------------------------

@pytest.mark.parametrize("task", ["td-auto-1a2b3c4d", "td-auto-1793613610", "td-", "TD-1", "td-1234567890", "free text"])
def test_a_task_id_the_record_cant_hold_is_left_out_and_the_typing_goes_ahead(home, task):
    ef = se.begin(WS, "task", kind="owner_flag", ref="td-auto-1a2b3c4d", component="pulse", task=task)
    row = rows(home)[0]
    assert row["id"] == ef and row["target"] == {"source": "task", "session": WS}
    assert row["auth"]["ref"] == "td-auto-1a2b3c4d"  # what allowed it is still named


def test_a_task_id_the_record_can_hold_is_still_recorded(home):
    se.begin(WS, "task", kind="owner_flag", ref="td-12", component="pulse", task="td-12")
    assert rows(home)[0]["target"] == {"source": "task", "session": WS, "task": "td-12"}


# --- 2. the typing is counted and held by its text, for the sources that carry a person's or a script's text ---------

def go(source, digest, session=WS):
    return se.begin(session, source, kind="tap", ref="x:y", component="x", text_digest=digest)


def test_the_sources_that_carry_a_digest_are_the_three_whose_text_is_chosen_each_time():
    assert se.DIGEST_SOURCES == ("cli", "run-now", "merge")


@pytest.mark.parametrize("source", ["cli", "run-now", "merge"])
def test_four_different_texts_in_half_an_hour_are_all_typed(home, clock, source):
    for n in range(se.LOOP_MAX + 2):
        se.finish(go(source, f"{n:016x}"), "done")
        clock.t += 5
    assert [r["target"]["msg_digest"] for r in rows(home) if r["kind"] == "intent"][:2] == [f"{0:016x}", f"{1:016x}"]


@pytest.mark.parametrize("source", ["cli", "run-now", "merge"])
def test_the_same_text_is_still_stopped_the_fourth_time(home, clock, source):
    for _ in range(se.LOOP_MAX):
        se.finish(go(source, DIGEST_A), "done")
        clock.t += 5
    assert refusal(go, source, DIGEST_A).code == "loop"
    se.finish(go(source, DIGEST_B), "done")  # another text is another thing
    se.finish(go(source, DIGEST_A, WS2), "done")  # and another session


@pytest.mark.parametrize("source", ["cli", "run-now", "merge"])
def test_a_different_text_is_typed_after_one_nobody_could_tell_about_and_the_same_text_is_held(home, clock, source):
    se.finish(go(source, DIGEST_A), "unknown", error="timeout")
    clock.t += 600
    exc = refusal(go, source, DIGEST_A)
    assert exc.code == "unknown" and "couldn't tell if it arrived" in str(exc)
    se.finish(go(source, DIGEST_B), "done")
    clock.t += se.LOOP_SEC
    se.finish(go(source, DIGEST_A), "done")  # held for half an hour only


@pytest.mark.parametrize("source", ["nudge:stranded", "cleanup", "boot", "clear", "pulse", "restore", "task", "submit"])
def test_every_other_source_is_still_limited_by_source_and_session_whatever_digest_it_is_given(home, clock, source):
    """A runaway typing may vary its text, so these are counted by source and session alone."""
    for n in range(se.LOOP_MAX):
        se.finish(go(source, f"{n:016x}"), "done")
        clock.t += 5
    assert all("msg_digest" not in r["target"] for r in rows(home) if r["kind"] == "intent")
    assert refusal(go, source, DIGEST_B).code == "loop"


@pytest.mark.parametrize("source", ["nudge:stranded", "cleanup", "boot", "clear", "pulse", "restore"])
def test_every_other_windowed_source_is_still_held_after_an_unconfirmed_one_whatever_its_digest(home, clock, source):
    se.finish(go(source, DIGEST_A), "unknown", error="timeout")
    clock.t += 60
    assert refusal(go, source, DIGEST_B).code == "unknown"


def test_the_slack_message_source_is_not_given_a_digest(home):
    se.begin(WS, "slack-message", kind="tap", ref="slack:1793613610.000100", component="comms",
             slack_ts="1793613610.000100", anchor=T0 - 5, once_tap="slack:1793613610.000100", text_digest=DIGEST_A)
    assert "msg_digest" not in rows(home)[0]["target"]


@pytest.mark.parametrize("bad", ["", "not hex at all!!", "ABCDEF0123456789", "0123", "0" * 17])
def test_a_digest_that_cant_be_used_goes_ahead_under_the_old_key_and_never_refuses(home, bad):
    se.begin(WS, "cli", kind="tap", ref="x:y", component="x", text_digest=bad)
    assert rows(home)[0]["target"] == {"source": "cli", "session": WS}


def test_the_digest_is_a_keyed_pointer_and_never_the_text(home):
    assert se.typed_digest(SECRET_TEXT) == se.typed_digest(SECRET_TEXT) != se.typed_digest(SECRET_TEXT + "x")
    assert HEX16.fullmatch(se.typed_digest(SECRET_TEXT))
    se.begin(WS, "cli", kind="tap", ref="x:y", component="x", text_digest=se.typed_digest(SECRET_TEXT))
    assert SECRET_TEXT not in (home / ".assistant/effects/effects.jsonl").read_text()
    # keyed: another install's secret gives another digest for the same text, and so no list of guesses matches it
    (home / ".assistant/feedback/sender.key").write_text("k" * 64)
    assert se.typed_digest(SECRET_TEXT) != se.typed_digest(SECRET_TEXT + "x")
    other = se.typed_digest(SECRET_TEXT)
    (home / ".assistant/feedback/sender.key").write_text("j" * 64)
    assert se.typed_digest(SECRET_TEXT) != other


def test_a_digest_that_cant_be_made_is_empty_not_an_error(home, monkeypatch):
    assert se.typed_digest(None) == "" and se.typed_digest(b"x") == ""
    monkeypatch.setattr(feedback, "_key", lambda: b"")  # no secret
    assert se.typed_digest("x") == ""
    monkeypatch.setattr(feedback, "_key", mock.Mock(side_effect=OSError("disk")))
    assert se.typed_digest("x") == ""
    monkeypatch.setattr(feedback, "text_pointer", mock.Mock(side_effect=RuntimeError("boom")))
    assert se.typed_digest("x") == ""
    monkeypatch.setattr(feedback, "text_pointer", lambda kind, text: "not hex")
    assert se.typed_digest("x") == ""


def test_the_feedback_pointer_for_a_text_is_the_same_each_time_and_not_the_senders_pointer(home):
    assert feedback.text_pointer("a", "x") == feedback.text_pointer("a", "x")
    assert HEX16.fullmatch(feedback.text_pointer("a", "x"))
    assert feedback.text_pointer("a", "x") != feedback.text_pointer("b", "x")
    assert feedback.text_pointer("typed", "x") != feedback.who_pointer("x")[len("who:"):]


def test_the_ledger_takes_a_text_digest_only_through_its_own_argument_and_only_as_a_digest(home):
    target = {"source": "cli", "session": WS}
    with pytest.raises(ValueError):
        effects.begin(se.CLASS, {**target, "msg_digest": DIGEST_A}, {"kind": "tap", "ref": "x:y", "at": "2026-11-01T00:00:00Z"})
    auth = {"kind": "tap", "ref": "x:y", "at": effects._iso(effects._now())}
    for bad in ("zz", "A" * 16, 5, "0" * 17, "x" + "0" * 16):
        with pytest.raises(ValueError):
            effects.begin(se.CLASS, target, auth, text_digest=bad)
    # a conversation's own digest (a long Gmail thread) can't be replaced by one a caller gives
    with pytest.raises(ValueError):
        effects.begin("gmail.trash", {"account": "me@example.com", "msgs": [f"m{n}" for n in range(150)]}, auth,
                      text_digest=DIGEST_A)
    effects.begin(se.CLASS, target, auth, text_digest=DIGEST_A)
    assert rows(home)[0]["target"] == {**target, "msg_digest": DIGEST_A}
