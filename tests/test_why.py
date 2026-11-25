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
    steps = why.steps_of([record], **kw)
    assert len(steps) == 1, (record, steps)
    return steps[0]["text"]


def effect(cls, outcome="done", **kw):
    return {"id": "ef-" + "0" * 16, "class": cls, "authorized_by": {"kind": "tap"}, "t": NOW, "finished_t": NOW + 1,
            "outcome": outcome, "verified": True, "error": "", "undoes": "", **kw}


def text_of(e, now=NOW + 5):
    return why.steps_of([], [e], now=now)[0]["text"]


# --- the sentences ----------------------------------------------------------------------------------------------

def test_a_first_sighting_is_the_oldest_step_says_where_when_a_record_does_and_comes_only_when_the_time_is_known():
    steps = why.steps_of([ev("task", NOW, "td-1", state="done", prev_state="open")], first=NOW - 100, now=NOW + 1)
    assert [(s["kind"], s["text"]) for s in steps] == [("first_seen", "Assistant first saw this."),
                                                      ("task", "The agent reported it finished.")]
    assert why.steps_of([], first=None) == []
    for source, name in (("github", "GitHub"), ("slack", "Slack"), ("gmail", "Gmail"), ("outlook", "Outlook")):
        assert why.steps_of([], first=NOW, now=NOW + 1, source=name)[0]["text"] == f"Assistant first saw this, in {name}."


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


def test_a_sorted_row_names_the_model_and_the_sorting_rules_only_when_telemetry_takes_them_as_names():
    base = {"verdict": "you", "lane": "do_first"}
    text = one(ev("triage_verdict", NOW, model="claude-sonnet-4", prompt_version="rubric-1a2b3c4d", **base))
    assert text.endswith(" Model: claude-sonnet-4. Sorting rules: version 1a2b3c4d.")
    assert one(ev("triage_verdict", NOW, model="claude-sonnet-4", **base)).endswith(" Model: claude-sonnet-4.")
    assert one(ev("triage_verdict", NOW, prompt_version="other-rules", **base)).endswith(" Sorting rules: other-rules.")
    for odd in ("two words <b>", 7, "person@example.com", "-".join(["xoxb", "1234", "5678", "abcdefghijk"]),
                "https://evil.example/x", "Users/someone/secret.txt"):
        said = one(ev("triage_verdict", NOW, model=odd, prompt_version=odd, **base))
        assert "Model" not in said and "rules" not in said, odd


@pytest.mark.parametrize("reason, said", [
    ("call_failed", "the model's answer didn't come back"), ("bad_answer", "the model's answer couldn't be used"),
    ("not_answered", "the model left this one out of its answer"), ("out_of_time", "the sorting run ran out of time"),
    ("spend_limit", "today's spend limit for sorting was reached"), ("no_model", "no model is set up for sorting"),
])
def test_an_unsorted_row_says_why_in_one_clause_for_each_reason(reason, said):
    step = why.steps_of([ev("triage_verdict", NOW, verdict="unsorted", reason=reason)])[0]
    assert (step["kind"], step["text"]) == ("unsorted", f"The model didn't sort it: {said}.")


def test_an_unsorted_row_with_a_reason_nobody_knows_yet_is_still_said():
    assert one(ev("triage_verdict", NOW, verdict="unsorted", reason="a_new_one")) == "The model didn't sort it."


def test_a_task_made_from_the_row_says_how_it_was_sent_and_which_task_it_became():
    assert one(ev("task", NOW, "td-12", state="open", origin="work-queue", mode="assist")) == \
        "You sent it to an agent for a first pass. It became task td-12."
    assert one(ev("task", NOW, "td-12", state="open", origin="work-queue", mode="handsoff")) == \
        "You sent it to an agent, hands-off. It became task td-12."
    assert one(ev("task", NOW, "", state="open", origin="work-queue")) == "You sent it to an agent for a first pass."
    assert one(ev("task", NOW, "not-an-id", state="open", origin="work-queue")) == "You sent it to an agent for a first pass."


@pytest.mark.parametrize("origin, with_id, without", [
    ("manual", "Task td-12 was made for it.", "A task was made for it."),
    ("goal", "Assistant's goal planner made task td-12 for it.", "Assistant's goal planner made a task for it."),
    ("agent", "An agent made task td-12 for it.", "An agent made a task for it."),
    ("slack", "Task td-12 was made for it.", "A task was made for it."),
])
def test_a_task_made_by_someone_else_says_who_without_claiming_it_was_assistant_when_it_may_have_been_you(origin, with_id, without):
    assert one(ev("task", NOW, "td-12", state="open", origin=origin)) == with_id
    assert one(ev("task", NOW, "x" * 5, state="open", origin=origin)) == without
    assert one(ev("task", NOW, "td-12\n<b>", state="open", origin=origin)) == without, "an id that isn't one is not shown"


@pytest.mark.parametrize("state, said", [
    ("done", "The agent reported it finished."), ("blocked", "The agent reported it is blocked."),
    ("removed", "The task was removed."), ("deferred", "The task was put off."),
])
def test_a_task_changing_state_says_so(state, said):
    assert one(ev("task", NOW, "td-1", state=state, prev_state="in-progress")) == said


def test_a_task_coming_back_to_open_is_queued_again_and_one_marked_as_started_says_so_unless_a_start_just_said_it():
    assert one(ev("task", NOW, "td-1", state="open", prev_state="blocked")) == "The task was queued to start again."
    assert one(ev("task", NOW, "td-1", state="in-progress", prev_state="blocked")) == "The task was marked as started."
    start = ev("dispatch", NOW, "td-1", outcome="done", ok=True)
    after = ev("task", NOW + 0.01, "td-1", state="in-progress", prev_state="open")
    assert [s["text"] for s in why.steps_of([start, after])] == ["Assistant started an agent on it."]
    late = ev("task", NOW + why.JUST_AFTER + 1, "td-1", state="in-progress", prev_state="open")
    assert [s["text"] for s in why.steps_of([start, late])] == ["Assistant started an agent on it.", "The task was marked as started."]
    before = ev("task", NOW - 1, "td-1", state="in-progress", prev_state="open")
    assert len(why.steps_of([before, start])) == 2, "only a start that came first explains it"


def test_a_task_in_a_state_nobody_knows_is_left_out():
    assert why.steps_of([ev("task", NOW, "td-1", state="vanished", prev_state="open")]) == []


def test_a_start_says_whether_the_agent_got_the_instruction_and_leaves_the_workspace_id_out():
    assert one(ev("dispatch", NOW, "td-1", outcome="done", workspace="workspace:1000010013", confirmed=True)) == \
        "Assistant started an agent on it."
    assert one(ev("dispatch", NOW, "td-1", outcome="done", workspace="workspace:1000010013", confirmed=False)) == \
        "Assistant started an agent on it. It couldn't confirm the session got the instruction."


@pytest.mark.parametrize("reason, said", [
    ("session_tool_down", "Assistant tried to start an agent and couldn't: cmux didn't answer. It tries again on the next round."),
    ("no_workspace", "Assistant tried to start an agent and couldn't: cmux couldn't open a workspace for it. It tries again on the next round."),
    ("no_surface", "Assistant tried to start an agent and couldn't: the new workspace had no window to type in. It tries again on the next round."),
    ("todo_not_marked", "Assistant started an agent on it, but couldn't mark the task as started, so the next round may start a second one."),
    ("other", "Assistant tried to start an agent and couldn't: something went wrong."),
    ("typing_refused", "Assistant opened a session but wouldn't type into it, because it couldn't record what it types. The task is parked."),
    ("session_refused_text", "cmux refused the instruction, so nothing was typed. The task is parked."),
    ("a_value_nobody_knows", "Assistant tried to start an agent and couldn't."),
    (None, "Assistant tried to start an agent and couldn't."),
])
def test_a_failed_start_says_only_what_is_true_for_each_reason_and_promises_a_retry_only_where_there_is_one(reason, said):
    step = why.steps_of([ev("dispatch", NOW, "td-1", outcome="failed", reason=reason)])[0]
    assert (step["kind"], step["text"]) == ("start_failed", said)
    assert ("tries again" in said) == (reason in ("session_tool_down", "no_workspace", "no_surface"))


def test_a_start_that_stopped_because_the_session_was_not_ready_says_whether_the_task_was_parked():
    parked = one(ev("dispatch", NOW, "td-1", outcome="failed", reason="session_not_ready", workspace="workspace:1000010013"))
    assert parked == ("Assistant tried to start an agent and couldn't: the agent session wasn't ready for it. The task is "
                      "marked as started and Assistant won't start it again by itself. To run it again, remove the task "
                      "on the Tasks tab of the Assistant dashboard and send the row to an agent again.")
    retry = one(ev("dispatch", NOW, "td-1", outcome="failed", reason="session_not_ready"))
    assert retry == ("Assistant tried to start an agent and couldn't: the agent session wasn't ready for it. "
                     "It tries again on the next round.")


def test_every_reason_telemetry_knows_for_a_failed_start_has_its_own_sentence():
    assert set(telemetry.DISPATCH_REASON) - {"session_not_ready"} <= set(why.START_FAILED)


def test_another_outcome_of_a_start_is_left_out():
    assert why.steps_of([ev("dispatch", NOW, "td-1", outcome="skipped")]) == []


def test_a_check_names_what_it_found_and_an_unknown_check_is_shown_by_its_name_when_telemetry_takes_it_as_one():
    assert one(ev("check", NOW, "td-1", name="pr-merged")) == "Assistant checked the result: the pull request is merged."
    assert one(ev("check", NOW, "td-1", name="pr-closed")).endswith("the pull request is merged or closed.")
    assert "you commented on or reviewed" in one(ev("check", NOW, "td-1", name="you-commented"))
    assert "you replied in its Slack thread" in one(ev("check", NOW, "td-1", name="you-replied"))
    assert "looks cleared" in one(ev("check", NOW, "td-1", name="unblocked"))
    assert one(ev("check", NOW, "td-1", name="new-check")) == "Assistant ran a check on the result (new-check)."
    assert why.steps_of([ev("check", NOW, "td-1", name="two words")]) == []
    assert why.steps_of([ev("check", NOW, "td-1", name="person@example.com")]) == []


def test_a_kind_nobody_knows_is_left_out_and_the_copies_of_the_other_records_are_not_shown_twice():
    assert why.steps_of([ev("source_fetch", NOW), ev("model_call", NOW), ev("effect", NOW), ev("feedback", NOW),
                         {"kind": "task", "ts": NOW, "subject": "", "fields": None}, "not a record",
                         {"kind": "task", "ts": "soon", "subject": "", "fields": {"state": "done"}}]) == []


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
    assert why.steps_of([], [open_now], now=NOW + 5)[0]["at"] == NOW


def test_a_change_begun_in_the_future_after_the_clock_went_back_is_not_known_to_be_in_progress():
    ahead = effect("gmail.archive", None, finished_t=None, t=NOW + 3 * why.IN_FLIGHT_SEC)
    assert text_of(ahead, now=NOW) == \
        "Tried to archive it in Gmail but didn't hear back, so it may or may not have gone through."
    near = effect("gmail.archive", None, finished_t=None, t=NOW + 5)
    assert text_of(near, now=NOW) == "Archiving it in Gmail is in progress."


def test_a_change_that_undoes_another_says_which_and_a_class_nobody_knows_gets_a_general_sentence():
    undo = effect("gmail.unarchive", id="ef-" + "1" * 16, undoes="ef-" + "0" * 16)
    steps = why.steps_of([], [effect("gmail.archive"), undo], now=NOW + 5)
    assert steps[1]["text"] == ("Moved back to your Gmail inbox, because you pressed the button. "
                                "This undid the earlier archive.")
    assert text_of(effect("gmail.something_new")) == "Assistant made a change outside itself because of it."


def test_a_step_with_a_time_that_cant_be_one_is_left_out():
    odd = [effect("gmail.archive", t=1e14, finished_t=1e14), effect("gmail.mark_read", t=float("inf"), finished_t=None),
           effect("gmail.trash", t=NOW, finished_t=NOW + 1)]
    assert [s["text"][:6] for s in why.steps_of([], odd, now=NOW + 5)] == ["Moved "]
    tomorrow = effect("gmail.archive", t=NOW + why.FUTURE_SEC + 10, finished_t=NOW + why.FUTURE_SEC + 10)
    assert why.steps_of([], [tomorrow], now=NOW) == []
    just = effect("gmail.archive", t=NOW + why.FUTURE_SEC - 10, finished_t=NOW + why.FUTURE_SEC - 10)
    assert len(why.steps_of([], [just], now=NOW)) == 1
    assert why.steps_of([ev("task", 0, "td-1", state="done", prev_state="open")]) == []


# --- what you said ----------------------------------------------------------------------------------------------

def say(signal, **kw):
    return {"id": "fb-" + "0" * 16, "ts": "2026-11-20T10:00:00Z", "signal": signal, "kind": "row", **kw}


@pytest.mark.parametrize("signal, said", [
    ("done", "You marked it done."), ("drop", "You dropped it."), ("snooze", "You snoozed it."),
    ("undo", "You undid your last change on it."), ("send", "You sent it to an agent for a first pass."),
    ("send_handsoff", "You sent it to an agent, hands-off."), ("approve", "You approved it."),
    ("keep", "You chose to keep it."), ("archive", "You archived it."), ("mark_read", "You marked it read."),
    ("used_draft", "You used the draft."), ("kept_result", "You marked the result done."),
    ("tell", "You told Assistant something about it."), ("retract", "You took something back."),
    ("thumbs_down", "You said this is wrong."),
])
def test_what_you_said_has_a_phrase_for_each_signal_the_record_knows(signal, said):
    steps = why.steps_of([], [], [say(signal)], now=1_800_000_000)
    assert (steps[0]["kind"], steps[0]["text"]) == ("feedback", said)


def test_every_signal_and_reason_the_feedback_record_knows_has_a_phrase():
    assert set(why.FEEDBACK) | {"thumbs_down", "retract"} == set(feedback.SIGNALS)
    assert set(why.SAID) == set(feedback.REASONS) | {"bad_draft"}
    assert set(why.TOOK_BACK) == set(feedback.TAKE_BACK)


@pytest.mark.parametrize("taken, said", [
    ("thumbs_down", "You took back your “this is wrong”."), ("tell", "You took back what you told Assistant."),
    ("used_draft", "You took back “used the draft”."), ("done", "You took something back.")])
def test_a_take_back_says_what_was_taken_back(taken, said):
    target = {**say(taken), "id": "fb-" + "1" * 16}
    steps = why.steps_of([], [], [target, say("retract", amends="fb-" + "1" * 16, ts="2026-11-20T10:05:00Z")], now=1_800_000_000)
    assert steps[-1]["text"] == said


def test_a_reason_a_draft_and_a_result_are_named_for_what_they_are_about():
    assert why.steps_of([], [], [say("thumbs_down", reason="not_important", amends="fb-1")], now=1_800_000_000)[0]["text"] == \
        "You said why: not important."
    assert why.steps_of([], [], [say("thumbs_down", kind="draft")], now=1_800_000_000)[0]["text"] == "You said this draft is wrong."
    assert why.steps_of([], [], [say("thumbs_down", kind="result")], now=1_800_000_000)[0]["text"] == "You said this result is wrong."


def test_feedback_nobody_knows_or_with_no_time_is_left_out():
    assert why.steps_of([], [], [say("a_new_signal")], now=1_800_000_000) == []
    assert why.steps_of([], [], [say("thumbs_down", reason="a_new_reason", amends="fb-1")], now=1_800_000_000) == []
    assert why.steps_of([], [], [say("done", ts="yesterday")], now=1_800_000_000) == []


def test_a_send_is_said_once_by_the_task_and_a_change_once_by_the_ledger():
    at = _epoch("2026-11-20T10:00:00Z")
    sent = ev("task", at + 1, "td-1", state="open", origin="work-queue", mode="assist")
    steps = why.steps_of([sent], [], [say("send"), say("send_handsoff"), say("done")], now=at + 100)
    assert sorted(s["text"] for s in steps if "agent" in s["text"]) == [
        "You sent it to an agent for a first pass. It became task td-1.",
        "You sent it to an agent, hands-off."], "one task: one tap is the task, and the other made nothing, so it is shown"
    # with no task record, what you pressed is still said
    assert why.steps_of([], [], [say("send")], now=at + 100)[0]["text"] == "You sent it to an agent for a first pass."


def _epoch(stamp):
    from datetime import datetime, timezone
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def test_a_tap_is_dropped_only_when_it_pairs_with_a_change_so_archive_undo_done_and_clear_show_the_last_undo():
    at = _epoch("2026-11-20T10:00:00Z")
    taps = [say("archive", ts="2026-11-20T10:00:01Z", id="fb-" + "1" * 16), say("undo", ts="2026-11-20T10:01:00Z", id="fb-" + "2" * 16),
            say("done", ts="2026-11-20T10:02:00Z", id="fb-" + "3" * 16), say("undo", ts="2026-11-20T10:03:00Z", id="fb-" + "4" * 16)]
    ledger = [effect("gmail.archive", t=at, finished_t=at + 1), effect("gmail.unarchive", id="ef-" + "1" * 16, t=at + 59, finished_t=at + 60,
                                                                        undoes="ef-" + "0" * 16)]
    steps = why.steps_of([], ledger, taps, now=at + 1000)
    assert [s["text"] for s in steps] == ["Archived in Gmail, because you pressed the button.",
                                         "Moved back to your Gmail inbox, because you pressed the button. This undid the earlier archive.",
                                         "You marked it done.", "You undid your last change on it."]


def test_two_taps_for_one_change_show_the_second_and_a_tap_far_from_the_change_is_not_paired_with_it():
    at = _epoch("2026-11-20T10:00:00Z")
    two = [say("archive", ts="2026-11-20T10:00:01Z"), say("archive", ts="2026-11-20T10:00:02Z", id="fb-" + "1" * 16)]
    steps = why.steps_of([], [effect("gmail.archive", t=at, finished_t=at + 1)], two, now=at + 100)
    assert [s["text"] for s in steps].count("You archived it.") == 1
    far = why.steps_of([], [effect("gmail.archive", t=at, finished_t=at + 1)], [say("archive", ts="2026-11-20T11:00:00Z")], now=at + 4000)
    assert [s["text"] for s in far].count("You archived it.") == 1


def test_the_steps_are_oldest_first_across_all_three_records():
    steps = why.steps_of(
        [ev("task", 30, "td-1", state="done", prev_state="open"), ev("check", 40, "td-1", name="pr-merged")],
        [effect("gmail.archive", t=50, finished_t=51)],
        [say("done", ts="1970-01-01T00:00:20Z"), say("thumbs_down", ts="1970-01-01T00:01:00Z")], first=10, now=100)
    assert [s["at"] for s in steps] == [10, 20, 30, 40, 51, 60]
    assert [s["kind"] for s in steps] == ["first_seen", "feedback", "task", "check", "effect", "feedback"]


# --- steps that repeat --------------------------------------------------------------------------------------------

def test_sixty_failed_starts_in_a_row_are_one_step_with_the_first_time_the_count_and_the_last_time():
    fails = [ev("dispatch", 100 + n, "td-1", outcome="failed", reason="session_tool_down") for n in range(60)]
    steps = why.steps_of(fails, now=NOW)
    assert len(steps) == 1
    assert steps[0] == {"at": 100.0, "kind": "start_failed", "count": 60, "last": 159.0,
                        "text": "Assistant tried to start an agent 60 times and couldn't: cmux didn't answer. It tries again on the next round."}


def test_a_different_step_in_between_ends_the_run_and_any_identical_steps_collapse_not_only_starts():
    fail = lambda n: ev("dispatch", n, "td-1", outcome="failed", reason="no_workspace")
    steps = why.steps_of([fail(1), fail(2), ev("check", 3, "td-1", name="pr-merged"), fail(4)], now=NOW)
    assert [(s["kind"], s.get("count")) for s in steps] == [("start_failed", 2), ("check", None), ("start_failed", None)]
    assert steps[2]["text"] == "Assistant tried to start an agent and couldn't: cmux couldn't open a workspace for it. It tries again on the next round."
    different = why.steps_of([fail(1), ev("dispatch", 2, "td-1", outcome="failed", reason="session_tool_down")], now=NOW)
    assert [s.get("count") for s in different] == [None, None], "two failures for different reasons are two steps"
    twice = why.steps_of([ev("check", 1, "td-1", name="pr-merged"), ev("check", 2, "td-1", name="pr-merged")], now=NOW)
    assert twice == [{"at": 1.0, "kind": "check", "count": 2, "last": 2.0,
                      "text": "Assistant checked the result: the pull request is merged. This happened 2 times."}]
    mixed = why.steps_of([ev("dispatch", 1, "td-1", outcome="failed", reason="todo_not_marked"),
                          ev("dispatch", 2, "td-1", outcome="failed", reason="todo_not_marked")], now=NOW)
    assert mixed[0]["text"].endswith("the next round may start a second one. This happened 2 times.")


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


def test_a_result_with_no_row_does_not_say_the_row_was_here_before_the_record(home):
    assert why.read(TRACE, first=None, row=False)["note"] == ""
    assert why.read(TRACE, first=None, row=True)["note"] == why.BEFORE_RECORDS


def test_a_full_read_gathers_the_three_records_and_leaves_the_telemetry_copies_out(home):
    telemetry.emit("triage_verdict", TRACE, "row:" + "1" * 16, "work-queue", verdict="you", lane="do_first", source="github")
    telemetry.emit("task", TRACE, "td-3", "work-queue", state="open", origin="work-queue", mode="assist")
    telemetry.emit("effect", TRACE, "ef-" + "2" * 16, "work-queue", action="gmail.archive", outcome="done", ok=True)
    e = effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"},
                      {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:10Z"}, trace=TRACE)
    effects.finish(e, "done", True)
    feedback.record("done", "row:" + "1" * 16, "row", "work-queue", trace=TRACE)
    feedback.emit_pending()
    answer = why.read(TRACE, first=NOW)
    assert answer["state"] == "ok" and answer["more"] == 0 and answer["left_out"] == ""
    assert [s["kind"] for s in answer["steps"]][0] == "first_seen"
    assert sorted(s["text"] for s in answer["steps"]) == sorted([
        "Assistant first saw this, in GitHub.", "The model sorted it into Do first and suggested you do it yourself.",
        "You sent it to an agent for a first pass. It became task td-3.",
        "Archived in Gmail, because you pressed the button.", "You marked it done."])


def test_only_the_newest_fifty_steps_are_shown_and_the_earlier_ones_are_counted(home):
    for n in range(60):
        telemetry.emit("task", TRACE, "td-1", "work-queue", state="done" if n % 2 else "blocked", prev_state="open")
    answer = why.read(TRACE)
    assert len(answer["steps"]) == 50 and answer["more"] == 10
    assert answer["left_out"] == "10 earlier steps aren't shown here."
    assert [s["at"] for s in answer["steps"]] == sorted(s["at"] for s in answer["steps"])
    everything = telemetry.events(trace=TRACE)
    assert answer["steps"][-1]["at"] == round(everything[-1]["ts"], 3), "the newest ones are the ones kept"
    assert answer["steps"][0]["at"] == round(everything[10]["ts"], 3)


def test_the_count_of_what_isnt_shown_is_singular_for_one_and_never_counts_records_with_no_sentence(home):
    assert why.answer("ok", more=1)["left_out"] == "1 earlier step isn't shown here."
    assert why.answer("ok", more=2, floor=True)["left_out"] == "At least 2 earlier steps aren't shown here."
    assert why.answer("ok", more=0, floor=True)["left_out"] == "Earlier steps may not be shown here."
    assert why.answer("ok")["left_out"] == ""
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="open", prev_state="done")
    telemetry.emit("job_run", TRACE, "", "pulse", job="pulse", outcome="done", ok=True)
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="vanished", prev_state="open")
    answer = why.read(TRACE)
    assert [s["text"] for s in answer["steps"]] == ["The task was queued to start again."]
    assert answer["more"] == 0 and answer["left_out"] == ""


def test_a_read_that_stopped_at_its_limit_says_at_least_and_the_limits_are_the_ones_written_here(home):
    for n in range(450):
        telemetry.emit("task", TRACE, "td-1", "work-queue", state="done" if n % 2 else "blocked", prev_state="open")
    answer = why.read(TRACE)
    assert why.EVENTS_READ == 400 and why.EFFECTS_READ == 100 and why.FEEDBACK_READ == 100 and why.SHOWN == 50
    assert answer["more"] == 350 and answer["left_out"] == "At least 350 earlier steps aren't shown here."


def test_a_store_that_is_there_but_cant_be_read_is_unreadable_and_never_nothing_recorded(home):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    path = home / ".assistant/telemetry/telemetry.db"
    path.write_bytes(b"not a database at all, just text" * 40)
    answer = why.read(TRACE, first=NOW)
    assert answer["state"] == "unreadable"
    assert answer["message"] == "Assistant can't read its record right now. Nothing is lost; try again in a minute."
    assert [s["text"] for s in answer["steps"]] == ["Assistant first saw this."], "what could be read is still shown"
    assert answer["footer"] == "Some of it couldn't be read, so this may be missing steps. The record stays on this Mac."


def test_a_ledger_that_cant_be_read_makes_the_answer_unreadable_and_a_ledger_not_made_yet_does_not(home):
    path = home / ".assistant/effects/effects.jsonl"
    assert why.read(TRACE, first=NOW)["state"] == "ok", "no ledger yet: nothing recorded, and nothing wrong"
    e = effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"},
                      {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:10Z"}, trace=TRACE)
    effects.finish(e, "done", True)
    assert [s["kind"] for s in why.read(TRACE, first=NOW)["steps"]] == ["first_seen", "effect"]
    effects._forget()
    path.chmod(0)
    try:
        answer = why.read(TRACE, first=NOW)
        assert answer["state"] == "unreadable" and "Some of it couldn't be read" in answer["footer"]
        assert [s["kind"] for s in answer["steps"]] == ["first_seen"]
    finally:
        path.chmod(0o600)


def test_a_feedback_file_that_cant_be_read_makes_the_answer_unreadable_and_one_not_made_yet_does_not(home):
    assert why.read(TRACE, first=NOW)["state"] == "ok"
    feedback.record("done", "row:" + "1" * 16, "row", "work-queue", trace=TRACE)
    feedback.emit_pending()
    path = home / ".assistant/feedback/feedback.jsonl"
    assert [s["kind"] for s in why.read(TRACE, first=NOW)["steps"]] == ["first_seen", "feedback"]
    feedback.forget()
    path.chmod(0)
    try:
        answer = why.read(TRACE)
        assert (answer["state"], answer["steps"]) == ("unreadable", []), "never 'nothing has been recorded'"
        assert answer["message"] == why.UNREADABLE
    finally:
        path.chmod(0o600)


def test_readable_is_true_for_a_missing_file_and_false_for_one_that_is_there_and_cant_be_read(home):
    assert effects.readable() is True and feedback.readable() is True
    effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"},
                  {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:10Z"})
    feedback.record("done", "row:" + "1" * 16, "row", "work-queue")
    assert effects.readable() is True and feedback.readable() is True
    effects._forget()
    feedback.forget()
    for path in (home / ".assistant/effects/effects.jsonl", home / ".assistant/feedback/feedback.jsonl"):
        path.chmod(0)
    try:
        assert effects.readable() is False and feedback.readable() is False
    finally:
        for path in (home / ".assistant/effects/effects.jsonl", home / ".assistant/feedback/feedback.jsonl"):
            path.chmod(0o600)


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


def test_every_record_is_read_with_a_limit_in_one_connection_and_reading_writes_nothing(home, monkeypatch):
    seen = {}
    real = telemetry.read_trace
    monkeypatch.setattr(telemetry, "read_trace", lambda trace, limit: (seen.setdefault("events", (trace, limit)), real(trace, limit))[1])
    monkeypatch.setattr(telemetry, "events", lambda **kw: pytest.fail("one read, from one connection"))
    monkeypatch.setattr(effects, "history", lambda **kw: seen.setdefault("effects", kw) and [])
    monkeypatch.setattr(feedback, "recent", lambda **kw: seen.setdefault("feedback", kw) and [])
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    before = sorted(str(p) for p in home.rglob("*"))
    why.read(TRACE)
    assert seen["events"] == (TRACE, 400)
    assert seen["effects"]["limit"] == 100 and seen["feedback"]["limit"] == 100
    assert sorted(str(p) for p in home.rglob("*")) == before


# --- the telemetry read that tells "nothing" from "can't read" ---------------------------------------------------

def test_read_trace_tells_each_case_apart_and_events_still_gives_an_empty_list_for_all_of_them(home):
    folder = home / ".assistant/telemetry"
    assert telemetry.read_trace(TRACE) == ("missing", []) and telemetry.events() == []
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    telemetry.emit("task", "tr-" + "cd" * 8, "td-2", "work-queue", state="done", prev_state="open")
    state, found = telemetry.read_trace(TRACE)
    assert state == "ok" and [r["subject"] for r in found] == ["td-1"] and len(telemetry.events()) == 2
    assert found == telemetry.events(trace=TRACE, newest_first=True), "the same records, in the same shape and order"
    assert telemetry.read_trace("tr-" + "ef" * 8) == ("ok", []), "a trace with nothing recorded is ok and empty"
    assert telemetry.read_trace(7)[0] == "ok" and telemetry.read_trace(TRACE, limit=0)[1] != []
    db = folder / "telemetry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version=999")
    conn.commit()
    conn.close()
    assert telemetry.read_trace(TRACE) == ("newer", []) and telemetry.events() == []
    db.unlink()
    for suffix in ("-wal", "-shm"):
        (folder / ("telemetry.db" + suffix)).unlink(missing_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA application_id=12345")
    conn.execute("CREATE TABLE events (x)")
    conn.commit()
    conn.close()
    assert telemetry.read_trace(TRACE) == ("foreign", []) and telemetry.events() == []
    db.write_bytes(b"garbage" * 100)
    assert telemetry.read_trace(TRACE) == ("unreadable", []) and telemetry.events() == []
    db.unlink()
    db.write_bytes(b"")
    assert telemetry.read_trace(TRACE) == ("missing", []), "a store with no records table yet has nothing recorded"


def test_a_store_that_stays_busy_is_unreadable_not_empty(home):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    holder = sqlite3.connect(str(home / ".assistant/telemetry/telemetry.db"), isolation_level=None)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert telemetry.read_trace(TRACE) == ("unreadable", [])
    finally:
        holder.close()


def test_read_trace_cannot_write_and_never_raises(home, monkeypatch):
    telemetry.emit("task", TRACE, "td-1", "work-queue", state="done", prev_state="open")
    seen = []
    real = sqlite3.connect

    class Spy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            seen.append(sql)
            return self.conn.execute(sql, *args)

        def close(self):
            self.conn.close()
    monkeypatch.setattr(telemetry, "_connect", lambda create=True: Spy(real(str(home / ".assistant/telemetry/telemetry.db"))))
    assert telemetry.read_trace(TRACE)[0] == "ok" and seen[0] == "PRAGMA query_only=ON"
    monkeypatch.setattr(telemetry, "_connect", lambda create=True: 1 / 0)
    assert telemetry.read_trace(TRACE) == ("unreadable", [])


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


def test_a_key_joining_an_id_takes_the_earliest_first_time_its_group_holds(tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DATA", str(tmp_path))
    monkeypatch.setattr(traces, "now", lambda: NOW)
    traces.path().write_text(json.dumps({"https://a": {"trace": TRACE, "seen": "2026-11-20", "first": 200.0},
                                         "https://b": {"trace": TRACE, "seen": "2026-11-20", "first": 100.0}}))
    traces.stamp([["https://a", "https://b", "https://c"]], "2026-11-21")
    assert [traces.first_seen(k) for k in ("https://a", "https://b", "https://c")] == [200.0, 100.0, 100.0]
