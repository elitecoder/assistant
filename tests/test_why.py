"""src/assistant/why.py: the steps one piece of work went through, in plain sentences, from the records on this Mac."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time

import pytest

from assistant import effects, feedback, telemetry, why
from work_queue import traces

TRACE = "tr-" + "ab" * 8
NOW = 1_795_000_000.0


def ev(kind, ts, subject="", **fields):
    return {"id": "x", "ts": ts, "kind": kind, "trace": TRACE, "subject": subject, "component": "", "fields": fields}


def one(record, **kw):
    steps, skipped = why.steps_of([record], **kw)
    assert skipped == 0, record
    return steps[0]["text"]


def effect(cls, outcome="done", **kw):
    return {"id": "ef-" + "0" * 16, "class": cls, "authorized_by": {"kind": "tap"}, "t": NOW, "finished_t": NOW + 1,
            "outcome": outcome, "verified": True, "error": "", "undoes": "", **kw}


def text_of(e, now=NOW + 5):
    steps, _ = why.steps_of([], [e], now=now)
    return steps[0]["text"]


# --- the sentences ----------------------------------------------------------------------------------------------

def test_a_first_sighting_is_the_oldest_step_and_comes_only_when_the_time_is_known():
    steps, _ = why.steps_of([ev("task", NOW, "td-1", state="done", prev_state="open")], first=NOW - 100)
    assert [(s["kind"], s["text"]) for s in steps] == [("first_seen", "Assistant first saw this."),
                                                      ("task", "The agent reported it finished.")]
    assert why.steps_of([], first=None) == ([], 0)


@pytest.mark.parametrize("fields, said", [
    ({"verdict": "you", "lane": "do_first"}, "The model sorted it into Do first and suggested you do it yourself."),
    ({"verdict": "agent", "lane": "promise"}, "The model sorted it into Promise and said an agent can finish it."),
    ({"verdict": "handoff", "lane": "week"}, "The model sorted it into This week and suggested you hand it off."),
    ({"verdict": "drop", "lane": "optional"}, "The model sorted it into Optional and suggested you drop it."),
    ({"verdict": "you"}, "The model looked at it and suggested you do it yourself."),
    ({"verdict": "mystery", "lane": "nowhere"}, "The model looked at it."),
])
def test_a_sorted_row_says_the_lane_and_the_suggestion_with_the_page_s_own_words(fields, said):
    assert one(ev("triage_verdict", NOW, **fields)) == said


def test_a_sorted_row_names_the_model_and_the_rules_only_when_they_are_names():
    base = {"verdict": "you", "lane": "do_first"}
    text = one(ev("triage_verdict", NOW, model="claude-sonnet-4", prompt_version="rubric-1a2b3c4d", **base))
    assert text.endswith(" Model: claude-sonnet-4, rules: rubric-1a2b3c4d.")
    assert one(ev("triage_verdict", NOW, model="claude-sonnet-4", **base)).endswith(" Model: claude-sonnet-4.")
    assert one(ev("triage_verdict", NOW, prompt_version="rubric-1", **base)).endswith(" rules: rubric-1.")
    odd = one(ev("triage_verdict", NOW, model="two words <b>", prompt_version=7, **base))
    assert "Model" not in odd and "rules" not in odd


@pytest.mark.parametrize("reason, said", [
    ("call_failed", "the model's answer didn't come back"), ("bad_answer", "the model's answer couldn't be used"),
    ("not_answered", "the model left this one out of its answer"), ("out_of_time", "the sorting run ran out of time"),
    ("spend_limit", "today's spend limit for sorting was reached"), ("no_model", "no model is set up for sorting"),
])
def test_an_unsorted_row_says_why_in_one_clause_for_each_reason(reason, said):
    step = why.steps_of([ev("triage_verdict", NOW, verdict="unsorted", reason=reason)])[0][0]
    assert (step["kind"], step["text"]) == ("unsorted", f"The model didn't sort it: {said}.")


def test_an_unsorted_row_with_a_reason_nobody_knows_yet_is_still_said():
    assert one(ev("triage_verdict", NOW, verdict="unsorted", reason="a_new_one")) == "The model didn't sort it."


def test_a_task_made_from_the_row_says_how_it_was_sent_and_which_task_it_became():
    assert one(ev("task", NOW, "td-12", state="open", origin="work-queue", mode="assist")) == \
        "You sent it to an agent for a first pass. It became task td-12."
    assert one(ev("task", NOW, "td-12", state="open", origin="work-queue", mode="handsoff")) == \
        "You sent it to an agent, hands-off. It became task td-12."
    assert one(ev("task", NOW, "", state="open", origin="work-queue")) == "You sent it to an agent for a first pass."
    assert one(ev("task", NOW, "td-12", state="open", origin="goal")) == "Assistant made task td-12 for it."
    assert one(ev("task", NOW, "", state="open", origin="goal")) == "Assistant made a task for it."


@pytest.mark.parametrize("state, said", [
    ("done", "The agent reported it finished."), ("blocked", "The agent reported it is blocked."),
    ("removed", "The task was removed."), ("deferred", "The task was put off."),
])
def test_a_task_changing_state_says_so(state, said):
    assert one(ev("task", NOW, "td-1", state=state, prev_state="in-progress")) == said


def test_a_task_coming_back_to_open_is_queued_again_and_one_right_after_a_start_adds_nothing():
    assert one(ev("task", NOW, "td-1", state="open", prev_state="blocked")) == "The task was queued to start again."
    assert why.steps_of([ev("task", NOW, "td-1", state="in-progress", prev_state="open")]) == ([], 0)


def test_a_task_in_a_state_nobody_knows_is_left_out_and_counted():
    assert why.steps_of([ev("task", NOW, "td-1", state="vanished", prev_state="open")]) == ([], 1)


def test_a_start_says_where_and_whether_the_agent_got_the_instruction():
    assert one(ev("dispatch", NOW, "td-1", outcome="done", workspace="workspace:1000010013", confirmed=True)) == \
        "Assistant started an agent on it in workspace:1000010013."
    assert one(ev("dispatch", NOW, "td-1", outcome="done", workspace="workspace:1000010013", confirmed=False)) == \
        "Assistant started an agent on it in workspace:1000010013. It couldn't confirm the session got the instruction."
    assert one(ev("dispatch", NOW, "td-1", outcome="done", workspace="not a workspace")) == \
        "Assistant started an agent on it."


@pytest.mark.parametrize("reason, said", [
    ("session_tool_down", "the program that runs agent sessions didn't answer"),
    ("no_workspace", "it couldn't open a workspace for it"),
    ("no_surface", "the new workspace had no window to type in"),
    ("session_not_ready", "the agent session wasn't ready for it"),
    ("todo_not_marked", "it couldn't mark the task as started"),
    ("other", "something went wrong"),
])
def test_a_failed_start_gives_one_clause_for_each_reason_and_promises_a_retry(reason, said):
    step = why.steps_of([ev("dispatch", NOW, "td-1", outcome="failed", reason=reason)])[0][0]
    assert step["kind"] == "start_failed"
    assert step["text"] == f"Assistant tried to start an agent and couldn't: {said}. It tries again on the next round."


def test_a_failed_start_for_a_reason_added_later_gets_a_general_sentence_and_another_outcome_is_left_out():
    assert one(ev("dispatch", NOW, "td-1", outcome="failed", reason="brand_new")) == \
        "Assistant tried to start an agent and couldn't. It tries again on the next round."
    assert why.steps_of([ev("dispatch", NOW, "td-1", outcome="skipped")]) == ([], 1)


def test_a_check_names_what_it_found_and_an_unknown_check_is_shown_by_its_name_when_it_is_one():
    assert one(ev("check", NOW, "td-1", name="pr-merged")) == "Assistant checked the result: the pull request is merged."
    assert one(ev("check", NOW, "td-1", name="pr-closed")).endswith("the pull request is merged or closed.")
    assert "you commented on or reviewed" in one(ev("check", NOW, "td-1", name="you-commented"))
    assert "you replied in its Slack thread" in one(ev("check", NOW, "td-1", name="you-replied"))
    assert "looks cleared" in one(ev("check", NOW, "td-1", name="unblocked"))
    assert one(ev("check", NOW, "td-1", name="new-check")) == "Assistant checked the result: new-check."
    assert why.steps_of([ev("check", NOW, "td-1", name="two words")]) == ([], 1)


def test_a_kind_nobody_knows_is_left_out_and_counted_and_the_copies_of_the_other_records_are_not():
    steps, skipped = why.steps_of([ev("source_fetch", NOW), ev("model_call", NOW), ev("effect", NOW), ev("feedback", NOW),
                                   {"kind": "task", "ts": NOW, "subject": "", "fields": None}, "not a record"])
    assert (steps, skipped) == ([], 2)


# --- the ledger -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cls, said", [
    ("gmail.archive", "Archived in Gmail"), ("gmail.unarchive", "Moved back to your Gmail inbox"),
    ("gmail.mark_read", "Marked read in Gmail"), ("gmail.trash", "Moved to Gmail Trash"),
    ("gmail.untrash", "Taken out of Gmail Trash"), ("mail.unsubscribe", "Sent the unsubscribe request"),
    ("slack.save.complete", "Marked done in Slack"), ("slack.save.uncomplete", "Reopened in Slack"),
    ("session.type", "Typed into an agent session"),
])
def test_a_finished_change_says_what_was_done_for_each_class_in_the_ledger(cls, said):
    assert text_of(effect(cls)) == f"{said}, because you pressed the button."


def test_every_class_the_ledger_knows_has_a_phrase_and_every_kind_of_approval_has_one():
    assert set(why.EFFECTS) == set(effects.CLASSES)
    assert set(why.BECAUSE) == set(effects.KINDS)


@pytest.mark.parametrize("kind, said", [
    ("tap", "because you pressed the button."), ("decision", "because you approved it."),
    ("owner_flag", "because of a setting you turned on."), ("standing", "because of a standing rule you set.")])
def test_a_change_says_what_allowed_it(kind, said):
    assert text_of(effect("gmail.archive", authorized_by={"kind": kind})) == f"Archived in Gmail, {said}"


def test_a_change_with_an_authorization_nobody_knows_says_nothing_about_it_and_an_unconfirmed_one_says_so():
    assert text_of(effect("gmail.archive", authorized_by={"kind": "new"})) == "Archived in Gmail."
    assert text_of(effect("gmail.archive", authorized_by=None)) == "Archived in Gmail."
    assert text_of(effect("gmail.archive", verified=False)) == \
        "Archived in Gmail, because you pressed the button. It couldn't be confirmed."


@pytest.mark.parametrize("error, said", [("timeout", " (time-out)"), ("network", " (network problem)"),
                                         ("tool failed", " (the tool failed)"), ("http 503", " (error 503)"),
                                         ("other", ""), ("", ""), ("some words that are not on the list", "")])
def test_a_failed_change_says_how_and_never_repeats_an_error_text_it_doesnt_know(error, said):
    assert text_of(effect("gmail.archive", "failed", error=error)) == f"Tried to archive it in Gmail and it failed{said}."


def test_a_change_with_no_answer_may_or_may_not_have_happened_and_one_still_going_is_in_progress():
    assert text_of(effect("mail.unsubscribe", "unknown")) == \
        "Tried to send the unsubscribe request but didn't hear back, so it may or may not have gone through."
    open_now = effect("gmail.archive", None, finished_t=None)
    assert text_of(open_now, now=NOW + 5) == "Archiving it in Gmail is in progress."
    assert text_of(open_now, now=NOW + why.IN_FLIGHT_SEC) == \
        "Tried to archive it in Gmail but didn't hear back, so it may or may not have gone through."
    assert why.steps_of([], [open_now], now=NOW + 5)[0][0]["at"] == NOW


def test_a_change_that_undoes_another_says_which_and_a_class_nobody_knows_gets_a_general_sentence():
    undo = effect("gmail.unarchive", id="ef-" + "1" * 16, undoes="ef-" + "0" * 16)
    steps, _ = why.steps_of([], [effect("gmail.archive"), undo], now=NOW + 5)
    assert steps[1]["text"] == ("Moved back to your Gmail inbox, because you pressed the button. "
                                "This undid the earlier archive.")
    assert text_of(effect("gmail.something_new")) == "Assistant made a change outside itself because of it."


# --- what you said ----------------------------------------------------------------------------------------------

def say(signal, **kw):
    return {"id": "fb-" + "0" * 16, "ts": "2026-11-20T10:00:00Z", "signal": signal, "kind": "row", **kw}


@pytest.mark.parametrize("signal, said", [
    ("done", "You marked it done."), ("drop", "You dropped it."), ("snooze", "You snoozed it."),
    ("undo", "You undid your last change on it."), ("send", "You sent it to an agent for a first pass."),
    ("send_handsoff", "You sent it to an agent, hands-off."), ("approve", "You approved it."),
    ("keep", "You chose to keep it."), ("archive", "You archived it."), ("mark_read", "You marked it read."),
    ("used_draft", "You used the draft."), ("kept_result", "You marked the result done."),
    ("tell", "You told Assistant something about it."), ("retract", "You took that back."),
    ("thumbs_down", "You said this is wrong."),
])
def test_what_you_said_has_a_phrase_for_each_signal_the_record_knows(signal, said):
    steps, skipped = why.steps_of([], [], [say(signal)])
    assert (steps[0]["kind"], steps[0]["text"], skipped) == ("feedback", said, 0)


def test_every_signal_and_reason_the_feedback_record_knows_has_a_phrase():
    assert set(why.FEEDBACK) | {"thumbs_down"} == set(feedback.SIGNALS)
    assert set(why.SAID) == set(feedback.REASONS) | {"bad_draft"}


def test_a_reason_a_draft_and_a_result_are_named_for_what_they_are_about():
    assert why.steps_of([], [], [say("thumbs_down", reason="not_important", amends="fb-1")])[0][0]["text"] == \
        "You said why: not important."
    assert why.steps_of([], [], [say("thumbs_down", kind="draft")])[0][0]["text"] == "You said this draft is wrong."
    assert why.steps_of([], [], [say("thumbs_down", kind="result")])[0][0]["text"] == "You said this result is wrong."


def test_feedback_nobody_knows_or_with_no_time_is_left_out_and_counted():
    assert why.steps_of([], [], [say("a_new_signal")]) == ([], 1)
    assert why.steps_of([], [], [say("thumbs_down", reason="a_new_reason", amends="fb-1")]) == ([], 1)
    assert why.steps_of([], [], [say("done", ts="yesterday")]) == ([], 1)


def test_a_send_is_said_once_by_the_task_and_a_change_is_said_once_by_the_ledger():
    sent = ev("task", NOW, "td-1", state="open", origin="work-queue", mode="assist")
    steps, _ = why.steps_of([sent], [], [say("send"), say("send_handsoff"), say("done")])
    assert [s["text"] for s in steps].count("You sent it to an agent for a first pass. It became task td-1.") == 1
    assert [s["text"] for s in steps if "agent" in s["text"]] == ["You sent it to an agent for a first pass. It became task td-1."]
    steps, _ = why.steps_of([], [effect("gmail.archive")], [say("archive"), say("mark_read"), say("undo")])
    assert [s["text"] for s in steps] == ["Archived in Gmail, because you pressed the button.", "You marked it read.",
                                         "You undid your last change on it."]
    # with no task record, what you pressed is still said
    assert why.steps_of([], [], [say("send")])[0][0]["text"] == "You sent it to an agent for a first pass."


def test_the_steps_are_oldest_first_across_all_three_records():
    steps, _ = why.steps_of(
        [ev("task", 30, "td-1", state="done", prev_state="open"), ev("check", 40, "td-1", name="pr-merged")],
        [effect("gmail.archive", t=50, finished_t=51)],
        [say("done", ts="1970-01-01T00:00:20Z"), say("thumbs_down", ts="1970-01-01T00:01:00Z")], first=10)
    assert [s["at"] for s in steps] == [10, 20, 30, 40, 51, 60]
    assert [s["kind"] for s in steps] == ["first_seen", "feedback", "task", "check", "effect", "feedback"]


# --- the reader -------------------------------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASSISTANT_TELEMETRY_STRICT", raising=False)
    (tmp_path / ".assistant").mkdir()
    feedback.forget()
    return tmp_path


def test_a_trace_that_is_not_one_gets_none_without_touching_a_record(home):
    for bad in ("", "nope", None, 7, "tr-" + "AB" * 8):
        assert why.read(bad) == why.empty("none")
    assert why.empty("none")["message"] == "Assistant has no record for this yet. It starts one at the next refresh."
    assert not (home / ".assistant/telemetry").exists()


def test_a_row_with_an_id_and_nothing_recorded_says_so_in_its_own_words_and_a_row_from_before_says_that(home):
    answer = why.read(TRACE, first=None)
    assert (answer["state"], answer["steps"], answer["message"]) == (
        "none", [], "Nothing has been recorded about this since Assistant started keeping this record.")
    assert answer["note"] == why.BEFORE_RECORDS == (
        "This row was already here when Assistant started keeping this record, so earlier steps aren't shown.")
    known = why.read(TRACE, first=NOW)
    assert (known["state"], [s["text"] for s in known["steps"]], known["note"]) == (
        "ok", ["Assistant first saw this."], "")
    assert known["footer"] == "This is everything Assistant has recorded about this item. The record stays on this Mac."


def test_a_full_read_gathers_the_three_records_and_leaves_the_telemetry_copies_out(home):
    telemetry.emit("triage_verdict", TRACE, "row:" + "1" * 16, "work-queue", verdict="you", lane="do_first")
    telemetry.emit("task", TRACE, "td-3", "work-queue", state="open", origin="work-queue", mode="assist")
    telemetry.emit("effect", TRACE, "ef-" + "2" * 16, "work-queue", action="gmail.archive", outcome="done", ok=True)
    feedback.record("done", "row:" + "1" * 16, "row", "work-queue", trace=TRACE)
    feedback.emit_pending()
    cls = "gmail.archive"
    e = effects.begin(cls, {"account": "me@example.com", "thread": "t1"}, {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:10Z"}, trace=TRACE)
    effects.finish(e, "done", True)
    answer = why.read(TRACE, first=NOW)
    assert answer["state"] == "ok" and answer["more"] == 0
    assert [s["kind"] for s in answer["steps"]][0] == "first_seen"
    assert sorted(s["text"] for s in answer["steps"]) == sorted([
        "Assistant first saw this.", "The model sorted it into Do first and suggested you do it yourself.",
        "You sent it to an agent for a first pass. It became task td-3.",
        "Archived in Gmail, because you pressed the button.", "You marked it done."])


def test_only_the_newest_fifty_steps_are_shown_and_the_rest_are_counted(home):
    for n in range(60):
        telemetry.emit("task", TRACE, "td-1", "work-queue", state="done" if n % 2 else "blocked", prev_state="open")
    answer = why.read(TRACE)
    assert len(answer["steps"]) == 50 and answer["more"] == 10
    assert answer["left_out"] == "10 more steps aren't shown here."
    assert [s["at"] for s in answer["steps"]] == sorted(s["at"] for s in answer["steps"])
    everything = telemetry.events(trace=TRACE)
    assert answer["steps"][-1]["at"] == round(everything[-1]["ts"], 3), "the newest ones are the ones kept"
    assert answer["steps"][0]["at"] == round(everything[10]["ts"], 3)


def test_records_with_no_sentence_add_to_the_count_of_what_is_left_out(home):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="deferred", prev_state="open")
    telemetry.emit("job_run", TRACE, "", "pulse", job="pulse", outcome="done", ok=True)
    answer = why.read(TRACE)
    assert [s["text"] for s in answer["steps"]] == ["The task was put off."] and answer["more"] == 1


def test_a_store_that_is_there_but_cant_be_read_is_unreadable_and_never_nothing_recorded(home):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    path = home / ".assistant/telemetry/telemetry.db"
    path.write_bytes(b"not a database at all, just text" * 40)
    answer = why.read(TRACE, first=NOW)
    assert answer["state"] == "unreadable"
    assert answer["message"] == "Assistant can't read its record right now. Nothing is lost; try again in a minute."
    assert [s["text"] for s in answer["steps"]] == ["Assistant first saw this."], "what could be read is still shown"


@pytest.mark.parametrize("broken", ["telemetry", "effects", "feedback"])
def test_a_record_that_cannot_be_loaded_makes_the_answer_unreadable_and_not_an_error(home, monkeypatch, broken):
    import assistant
    monkeypatch.delattr(assistant, broken, raising=False)  # a `from assistant import x` finds an attribute before it looks
    monkeypatch.setitem(sys.modules, "assistant." + broken, None)
    answer = why.read(TRACE, first=NOW)
    assert answer["state"] == "unreadable" and answer["message"] == why.UNREADABLE
    assert answer["steps"][0]["text"] == "Assistant first saw this."


def test_a_read_that_fails_in_a_way_nobody_planned_for_is_unreadable_too(home, monkeypatch):
    monkeypatch.setattr(why, "_read", lambda *a: 1 / 0)
    assert why.read(TRACE) == why.empty("unreadable")


def test_every_record_is_read_with_a_limit_and_reading_writes_nothing(home, monkeypatch):
    seen = {}
    real = telemetry.events
    monkeypatch.setattr(telemetry, "events", lambda **kw: seen.setdefault("events", kw) and real(**kw))
    monkeypatch.setattr(effects, "history", lambda **kw: seen.setdefault("effects", kw) and [])
    monkeypatch.setattr(feedback, "recent", lambda **kw: seen.setdefault("feedback", kw) and [])
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    before = sorted(str(p) for p in home.rglob("*"))
    why.read(TRACE)
    assert seen["events"]["limit"] == why.EVENTS_READ and seen["events"]["newest_first"] is True
    assert seen["effects"]["limit"] == why.EFFECTS_READ and seen["feedback"]["limit"] == why.FEEDBACK_READ
    assert sorted(str(p) for p in home.rglob("*")) == before


# --- the telemetry read that tells "nothing" from "can't read" ---------------------------------------------------

def test_the_store_state_tells_each_case_apart_and_events_still_gives_an_empty_list_for_all_of_them(home):
    folder = home / ".assistant/telemetry"
    assert telemetry.store_state() == "missing" and telemetry.events() == []
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    assert telemetry.store_state() == "ok" and len(telemetry.events()) == 1
    db = folder / "telemetry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version=999")
    conn.commit()
    conn.close()
    assert telemetry.store_state() == "newer" and telemetry.events() == []
    db.unlink()
    for suffix in ("-wal", "-shm"):
        (folder / ("telemetry.db" + suffix)).unlink(missing_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA application_id=12345")
    conn.execute("CREATE TABLE events (x)")
    conn.commit()
    conn.close()
    assert telemetry.store_state() == "foreign" and telemetry.events() == []
    db.write_bytes(b"garbage" * 100)
    assert telemetry.store_state() == "unreadable" and telemetry.events() == []
    db.unlink()
    db.write_bytes(b"")
    assert telemetry.store_state() == "missing", "a store with no records table yet has nothing recorded"


def test_a_store_that_stays_busy_is_unreadable_not_empty(home, monkeypatch):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    holder = sqlite3.connect(str(home / ".assistant/telemetry/telemetry.db"), isolation_level=None)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert telemetry.store_state() == "unreadable"
    finally:
        holder.close()


# --- when a row was first seen -----------------------------------------------------------------------------------

def test_a_row_made_now_has_a_first_time_and_an_older_file_loads_without_one(tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DATA", str(tmp_path))
    monkeypatch.setattr(traces, "now", lambda: NOW)
    key = "https://github.com/o/r/pull/1"
    traces.stamp([[key]], "2026-11-20")
    assert traces.first_seen(key) == NOW and json.loads(traces.path().read_text())[key]["first"] == NOW
    traces.path().write_text(json.dumps({"https://old": {"trace": TRACE, "seen": "2026-11-20"}, "https://bare": TRACE,
                                         "https://odd": {"trace": TRACE, "seen": "2026-11-20", "first": "soon"},
                                         "https://neg": {"trace": TRACE, "seen": "2026-11-20", "first": -4},
                                         "https://flag": {"trace": TRACE, "seen": "2026-11-20", "first": True}}))
    assert [traces.first_seen(k) for k in ("https://old", "https://bare", "https://odd", "https://neg", "https://flag",
                                           "https://missing")] == [None] * 6
    traces.stamp([["https://old"]], "2026-11-21")
    assert "first" not in json.loads(traces.path().read_text())["https://old"], "an old row is not given a time it didn't have"


def test_a_first_time_survives_every_later_write_and_pruning_and_a_new_key_in_the_group_takes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DATA", str(tmp_path))
    clock = iter(range(1_700_000_000, 1_700_000_100))
    monkeypatch.setattr(traces, "now", lambda: float(next(clock)))
    key, other = "https://x/1", "https://x/2"
    traces.stamp([[key]], "2026-11-01")
    made = traces.first_seen(key)
    traces.stamp([[key, other]], "2026-11-02")
    traces.stamp([[key, other], ["https://x/3"]], "2026-11-03")
    assert traces.first_seen(key) == made and traces.first_seen(other) == made
    assert traces.first_seen("https://x/3") > made
    traces.trace_for(key)
    traces.prune({k: v for k, v in json.loads(traces.path().read_text()).items()}, "2026-11-04", set())
    assert traces.first_seen(key) == made
    assert traces.trace_for("https://x/new") and traces.first_seen("https://x/new") > made, "a send makes one too"


def test_a_row_given_the_id_a_sent_entry_holds_has_no_first_time(tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DATA", str(tmp_path))
    monkeypatch.setattr(traces, "now", lambda: NOW)
    (tmp_path / "queued.json").write_text(json.dumps({"https://x/1": {"id": "td-1", "trace": TRACE}}))
    traces.stamp([["https://x/1"]], "2026-11-01")
    assert traces.lookup("https://x/1") == TRACE and traces.first_seen("https://x/1") is None
    assert traces.trace_for("https://x/9", known=TRACE) == TRACE and traces.first_seen("https://x/9") is None


def test_an_unreadable_registry_has_no_first_time(tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DATA", str(tmp_path))
    traces.path().mkdir(parents=True)
    assert traces.first_seen("https://x") is None
