"""Why is this here? The steps one piece of work went through, in plain sentences, from the records Assistant keeps.

Three records speak about a piece of work, all filed under its trace id: the telemetry record (`telemetry.py`: what the
model made of it, the task it became, the agent started on it, the checks after), the effects ledger (`effects.py`: every
change Assistant made outside itself because of it) and the feedback record (`feedback.py`: what you said about it).
`read` gathers them and `steps_of` turns them into sentences, oldest first. This is the one place those sentences are
made: the Work queue server only calls it, and the page only shows what it gets (and formats the times).

What it says is built from fixed words, ids, states and times. A title, a sender, a message, a link, a path, the model's
reason or the words a person typed is never read from a record, so it can't be copied into an answer. A name that a
record holds (a model, a check) is shown only when telemetry itself would have accepted it as a name.

The answer is `{"state", "steps", "more", "message", "left_out", "note", "footer"}`. `state` is "ok", "none" (nothing is
recorded for it) or "unreadable" (a record couldn't be read, so what is shown may be missing something: it is never
"nothing recorded"). Each step is `{"at", "kind", "text"}`, and a step that happened several times in a row is one
step with `count` and `last` (the time of the last). A record of a kind or a value this module doesn't know is never an
error: it is left out, or given a general sentence where that reads true. `more` counts steps, never records.

Standard library only at the top: each record is imported where it is read, so one that can't be loaded makes the answer
"unreadable" and nothing worse. Reading never writes, and every read is bounded.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

SHOWN = 50  # the newest steps shown; the count of the rest is `more`
EVENTS_READ = 400
EFFECTS_READ = 100
FEEDBACK_READ = 100
FUTURE_SEC = 86400  # a step further ahead than this is a clock gone wrong, and is left out
PAIR_SEC = 5  # a thing you pressed and the change it made are recorded this close together

FOOTER = "This is everything Assistant has recorded about this item. The record stays on this Mac."
PARTIAL_FOOTER = "Some of it couldn't be read, so this may be missing steps. The record stays on this Mac."
CUT_FOOTER = "Only the newest records were read, so older steps may be missing. The record stays on this Mac."
NONE_YET = "Assistant has no record for this yet. It starts one at the next refresh."
NOTHING_SINCE = "Nothing has been recorded about this since Assistant started keeping this record."
UNREADABLE = "Assistant can't read its record right now. Nothing is lost; try again in a minute."
BEFORE_RECORDS = ("This row was already here when Assistant started keeping this record, so earlier steps "
                  "aren't shown.")

LANES = {"do_first": "Do first", "promise": "Promise", "week": "This week", "optional": "Optional"}
SOURCES = {"slack": "Slack", "gmail": "Gmail", "calendar": "Calendar", "outlook": "Outlook", "github": "GitHub"}
SUGGESTS = {"you": "suggested you do it yourself", "agent": "said an agent can finish it",
            "handoff": "suggested you hand it off", "drop": "suggested you drop it"}
UNSORTED = {"call_failed": "the model's answer didn't come back",
            "bad_answer": "the model's answer couldn't be used",
            "not_answered": "the model left this one out of its answer",
            "out_of_time": "the sorting run ran out of time",
            "spend_limit": "today's spend limit for sorting was reached",
            "no_model": "no model is set up for sorting"}
STARTING = "Assistant tried to start an agent and couldn't"
RETRY = "It will try again by itself."
# A task that was parked is marked as started, so nothing starts it again by itself. What does start it again is
# removing it on the Tasks tab and sending the row to an agent again (tests/test_why_restart.py); "Queue to start"
# on a task that is marked as started does not.
PARKED_HOW = ("The task is marked as started and Assistant won't start it again by itself. To run it again, remove "
              "{task} on the Tasks tab of the Assistant dashboard and send the row to an agent again.")
START_FAILED = {"session_tool_down": f"{STARTING}: cmux didn't answer. {RETRY}",
                "no_workspace": f"{STARTING}: cmux couldn't open a workspace for it. {RETRY}",
                "no_surface": f"{STARTING}: the new workspace had no window to type in. {RETRY}",
                "todo_not_marked": ("Assistant started an agent on it, but couldn't mark the task as started, so "
                                    "it may start a second one by itself."),
                "other": f"{STARTING}: something went wrong."}
# A start that opened a session and then wouldn't, or couldn't, type into it is stamped blocked by the pulse, which is
# the one status "Queue to start" on the dashboard turns back into an open task (tests/test_session_effects_pulse.py).
# `typing_refused` is also what the pulse records when it can't write what it types before any session is opened;
# the task then stays open and the next round tries again. The record shows which: a workspace only when it was stamped.
BLOCKED_HOW = ('The task is marked blocked. To run it again, close the workspace it opened and press "Queue to start" '
               "on {task} on the Tasks tab of the Assistant dashboard.")
# A `typing_refused` start with no workspace on its record either never opened a session (nothing was typed, so no
# typing is shown for it) or opened one and failed to stamp the task; the typings shown for it tell which.
NOT_STARTED = "Assistant couldn't record what it types, so it didn't start an agent. It will try again by itself."
OPENED_NOT_TYPED = ("Assistant opened a session but didn't type the task's instruction into it, because it couldn't "
                    "record what it types. It will try again by itself.")
REFUSED = {"typing_refused": ("Assistant opened a session but wouldn't type the task's instruction into it, because it "
                              "couldn't record what it types.", NOT_STARTED),
           "session_refused_text": ("cmux refused the instruction, so nothing was typed.",
                                    "cmux refused the instruction, so nothing was typed. It will try again by itself.")}
# What a failed start says it did about what was typed, so its typings need not be shown again: only a typing that
# failed, and only of these sources.
ACCOUNTS_FOR = {"typing_refused": ("task",), "session_refused_text": ("task",)}
TASK_SAYS = {"done": "The agent reported it finished.", "blocked": "The agent reported it is blocked.",
             "removed": "The task was removed.", "deferred": "The task was put off."}
# Who made a task that wasn't made from the row. `manual` is a person, or an agent running the /todo skill.
MADE_BY = {"manual": ("Task {task} was made for it.", "A task was made for it."),
           "goal": ("Assistant's goal planner made task {task} for it.", "Assistant's goal planner made a task for it."),
           "agent": ("An agent made task {task} for it.", "An agent made a task for it.")}
CHECKS = {"pr-merged": "Assistant checked the result: the pull request is merged.",
          "pr-closed": "Assistant checked the result: the pull request is merged or closed.",
          "you-commented": ("Assistant checked the result: you commented on or reviewed the pull request after the "
                            "agent finished."),
          "you-replied": "Assistant checked the result: you replied in its Slack thread after the agent finished.",
          "unblocked": "Assistant checked the result: what the agent was blocked on looks cleared."}
# For each kind of change Assistant makes: what it says when done, what it tried, what it is doing, what it undoes.
EFFECTS = {
    "gmail.archive": ("Archived in Gmail", "archive it in Gmail", "Archiving it in Gmail", "archive"),
    "gmail.unarchive": ("Moved back to your Gmail inbox", "move it back to your Gmail inbox",
                        "Moving it back to your Gmail inbox", "move back to the inbox"),
    "gmail.mark_read": ("Marked read in Gmail", "mark it read in Gmail", "Marking it read in Gmail", "mark as read"),
    "gmail.trash": ("Moved to Gmail Trash", "move it to Gmail Trash", "Moving it to Gmail Trash", "move to Trash"),
    "gmail.untrash": ("Taken out of Gmail Trash", "take it out of Gmail Trash", "Taking it out of Gmail Trash",
                      "move out of Trash"),
    "mail.unsubscribe": ("Sent the unsubscribe request", "send the unsubscribe request",
                         "Sending the unsubscribe request", "unsubscribe request"),
    "slack.save.complete": ("Marked done in Slack", "mark it done in Slack", "Marking it done in Slack",
                            "mark as done in Slack"),
    "slack.save.uncomplete": ("Reopened in Slack", "reopen it in Slack", "Reopening it in Slack",
                              "reopening in Slack"),
    "session.type": ("Typed into an agent session", "type into an agent session", "Typing into an agent session",
                     "typing into the session"),
}
BECAUSE = {"tap": "because you pressed the button", "decision": "because you approved it",
           "owner_flag": "because of a setting you turned on", "standing": "because of a standing rule you set"}
ERRORS = {"timeout": "time-out", "network": "network problem", "tool failed": "the tool failed",
          "bad answer": "unexpected answer", "not confirmed": "not confirmed", "not recorded": "not recorded"}
SAID = {"not_important": "not important", "wrong_order": "wrong order", "not_mine": "not mine",
        "already_done": "already done", "wrong_tone": "wrong tone", "wrong_facts": "wrong facts",
        "not_how_id_say": "not how I'd say it", "not_what_i_asked": "not what I asked",
        "didnt_finish": "it didn't finish", "should_not_have_started": "should not have been started",
        "should_not_have_pinged": "should not have pinged me", "bad_draft": "this draft is bad"}
FEEDBACK = {"done": "You marked it done.", "drop": "You dropped it.", "snooze": "You snoozed it.",
            "undo": "You undid your last change on it.", "send": "You sent it to an agent for a first pass.",
            "send_handsoff": "You sent it to an agent, hands-off.", "approve": "You approved it.",
            "keep": "You chose to keep it.", "archive": "You archived it.", "mark_read": "You marked it read.",
            "used_draft": "You used the draft.", "kept_result": "You marked the result done.",
            "tell": "You told Assistant something about it."}
TOOK_BACK = {"thumbs_down": "You took back your “this is wrong”.", "tell": "You took back what you told Assistant.",
             "used_draft": "You took back “used the draft”."}
# What a feedback record is about, when it is not the row itself.
WHAT = {"draft": "this draft", "result": "this result"}
EFFECT_OF_SIGNAL = {"archive": "gmail.archive", "mark_read": "gmail.mark_read", "undo": "gmail.unarchive"}
SIGNAL_OF_EFFECT = {v: k for k, v in EFFECT_OF_SIGNAL.items()}
HTTP = re.compile(r"http ([1-5][0-9]{2})")
RULES = re.compile(r"rubric-([0-9a-f]{4,16})")
TASK_ID = re.compile(r"td-[0-9]{1,9}")
WORKSPACE = re.compile(r"workspace:[0-9]{1,18}")
IN_FLIGHT_SEC = 600  # as in effects.py: an effect with no result for this long is no longer "in progress"


def _name(value) -> str:
    """A name a record holds (a model, a rules version, a check), or "" unless telemetry itself would take it as one."""
    try:
        from assistant import records
        return records.name_of(value) or ""
    except Exception:  # noqa: BLE001 - no way to check it, so it is not shown
        return ""


def _step(at, kind: str, text: str, store: str = "") -> dict:
    """A step. `store` (which record it came from) is for `_collapse` and never leaves this module."""
    return {"at": round(float(at), 3), "kind": kind, "text": text, "store": store}


# --- one telemetry record --------------------------------------------------------------------------------------

def _verdict(fields: dict) -> str:
    if fields.get("verdict") == "unsorted":
        why = UNSORTED.get(fields.get("reason"))
        return "The model didn't sort it" + (f": {why}." if why else ".")
    lane, says = LANES.get(fields.get("lane")), SUGGESTS.get(fields.get("verdict"))
    text = "The model " + (f"sorted it into {lane}" if lane else "looked at it")
    text += f" and {says}." if says else "."
    model, rules = _name(fields.get("model")), _name(fields.get("prompt_version"))
    text += f" Model: {model}." if model else ""
    if rules:
        version = RULES.fullmatch(rules)
        text += f" Sorting rules: {'version ' + version.group(1) if version else rules}."
    return text


def _task(record: dict) -> str | None:
    fields, task = record["fields"], record["subject"] if TASK_ID.fullmatch(record["subject"] or "") else ""
    state = fields.get("state")
    if state == "open" and not fields.get("prev_state"):
        origin = fields.get("origin")
        if origin != "work-queue":
            with_id, without = MADE_BY.get(origin, MADE_BY["manual"])
            return with_id.format(task=task) if task else without
        how = ", hands-off" if fields.get("mode") == "handsoff" else " for a first pass"
        return f"You sent it to an agent{how}." + (f" It became task {task}." if task else "")
    if state == "open":
        return "The task was queued to start again."
    if state == "in-progress":
        return "The task was marked as started."
    return TASK_SAYS.get(state)


def _dispatch(record: dict, typed=None, alone: bool = False) -> str | None:
    """`typed` is the typings the ledger holds for this start, by source (see `_pair_typings`): a start that typed the
    task's instruction says so in the same sentence, and says it once whether the ledger or the start's own record is
    the one that couldn't tell whether it arrived."""
    fields = record["fields"]
    if fields.get("outcome") == "done":
        instruction = (typed or {}).get("task")
        if instruction is not None:
            if instruction.get("outcome") == "done" and fields.get("confirmed") is not False:
                return "Assistant started an agent on it and typed the task's instruction into the new session."
            return ("Assistant started an agent on it and typed the task's instruction, but couldn't tell if the "
                    "session received it. Look at the session to check.")
        return "Assistant started an agent on it." + (
            " It couldn't confirm the session got the instruction. Look at the session to check."
            if fields.get("confirmed") is False else "")
    if fields.get("outcome") != "failed":
        return None
    reason = fields.get("reason")
    if reason in REFUSED:
        parked = WORKSPACE.fullmatch(fields.get("workspace") or "") is not None
        task = record["subject"] if TASK_ID.fullmatch(record["subject"] or "") else ""
        said, retry = REFUSED[reason]
        if parked:
            return said + " " + BLOCKED_HOW.format(task=f"task {task}" if task else "the task")
        return OPENED_NOT_TYPED if reason == "typing_refused" and alone else retry
    if reason == "session_not_ready":  # parked only when the task was stamped, which the record shows as a workspace
        parked = WORKSPACE.fullmatch(fields.get("workspace") or "") is not None
        task = record["subject"] if TASK_ID.fullmatch(record["subject"] or "") else ""
        return f"{STARTING}: the agent session wasn't ready for it. " + (
            PARKED_HOW.format(task=f"task {task}" if task else "the task") if parked else RETRY)
    return START_FAILED.get(reason, f"{STARTING}.")


def _check(fields: dict) -> str | None:
    name = fields.get("name")
    if name in CHECKS:
        return CHECKS[name]
    shown = _name(name)
    return f"Assistant ran a check on the result ({shown})." if shown else None


EVENT_TEXT = {"triage_verdict": ("sorted", lambda r, typed, alone: _verdict(r["fields"])),
              "task": ("task", lambda r, typed, alone: _task(r)),
              "dispatch": ("started", lambda r, typed, alone: _dispatch(r, typed, alone)),
              "check": ("check", lambda r, typed, alone: _check(r["fields"]))}
# The telemetry copies of what the other two records hold (`effect`, `feedback`) have no entry above, so they say
# nothing: the ledger and the feedback record are the ones read.
JUST_AFTER = 10  # an `in-progress` this soon after a start is the start itself, already said


def _event_steps(events: list, typed=None, alone=()) -> list:
    """`typed` is {index in `events` of a dispatch record: the typings paired with that start, by source}."""
    typed = typed or {}
    steps, started = [], []
    for index, record in enumerate(events):
        kind = record.get("kind")
        if kind == "dispatch":
            started.append(record["ts"])
        kind_word, make = EVENT_TEXT.get(kind, (None, None))
        text = make(record, typed.get(index), index in alone) if make else None
        if kind == "task" and record["fields"].get("state") == "in-progress" and any(
                0 <= record["ts"] - t <= JUST_AFTER for t in started):
            continue
        if text is None:
            continue
        if kind == "dispatch" and record["fields"].get("outcome") == "failed":
            kind_word = "start_failed"
        elif kind == "triage_verdict" and record["fields"].get("verdict") == "unsorted":
            kind_word = "unsorted"
        steps.append(_step(record["ts"], kind_word, text, "events"))
    return steps


def _source(events: list, feedback: list) -> str:
    """Where the row came from, when a record says so: the name of the source."""
    said = [r["fields"].get("source") for r in events if r.get("kind") == "triage_verdict"]
    said += [(r.get("context") or {}).get("source") for r in feedback if isinstance(r.get("context"), dict)]
    return next((SOURCES[s] for s in said if s in SOURCES), "")


# --- the ledger -------------------------------------------------------------------------------------------------

# What each typing into a session says, by where the text came from (a fixed name the ledger holds): what was done,
# what was tried, and what is being done. A source not here says the general thing.
TYPED = {
    "task": ("typed the task's instruction into its session", "type the task's instruction into its session",
             "typing the task's instruction into its session"),
    "trust": ("answered the new session's folder-trust question", "answer the new session's folder-trust question",
              "answering the new session's folder-trust question"),
    "nudge:stranded": ("nudged the session because it looked stalled", "nudge the session because it looked stalled",
                       "nudging the session because it looked stalled"),
    "cleanup": ("typed /cleanup into the session", "type /cleanup into the session", "typing /cleanup into the session"),
    "merge": ("asked the session to merge its pull request", "ask the session to merge its pull request",
              "asking the session to merge its pull request"),
    "slack-message": ("typed your Slack message into its session", "type your Slack message into its session",
                      "typing your Slack message into its session"),
}
TYPED_ANY = ("typed into an agent session", "type into an agent session", "typing into an agent session")
START_SOURCES = ("task", "trust")  # what a start types, in the order it types them
START_WINDOW = 120  # a start's typings begin this long before its record at most, and no later than JUST_AFTER


def _typing_text(e: dict, now: float) -> str:
    source = (e.get("target") or {}).get("source")
    done, tried, doing = TYPED.get(source, TYPED_ANY)
    # Only a typing nobody described better says who started it: the others already say what happened.
    because = ", because you pressed the button" if source not in TYPED and (
        (e.get("authorized_by") or {}).get("kind") == "tap") else ""
    outcome = e.get("outcome")
    if outcome == "done":
        return f"Assistant {done}{because}."
    if outcome == "failed":
        return f"Assistant tried to {tried}, but it didn't go through."
    if outcome == "unknown":
        return f"Assistant {done}{because}, but couldn't tell if the session received it."
    if abs(float(e.get("t") or 0) - now) >= IN_FLIGHT_SEC:  # no result and not young: the intent may be all there is
        return f"Assistant tried to {tried} but didn't hear back, so it may or may not have gone through."
    return f"Assistant is {doing}{because}."


def _pair_typings(events: list, effects: list) -> tuple:
    """Pair each typing in the ledger with the start it belongs to, one to one: a typing for a task goes with the next
    start of that task (any outcome) after it, within START_WINDOW, and a start takes one typing of each source. Returns ({index of a dispatch record: {source: effect}}, the ids of the effects paired, the indexes of the starts that have a typing left to be shown on its own). A typing that
    pairs with no start is left for `_effect_steps` to show on its own."""
    starts = [(i, r["ts"], r.get("subject")) for i, r in enumerate(events) if r.get("kind") == "dispatch"]
    typed, claimed, alone = {}, set(), set()
    typings = sorted((e for e in effects if e.get("class") == "session.type" and isinstance(e.get("t"), (int, float))),
                     key=lambda e: e["t"])
    for e in typings:
        target = e.get("target") or {}
        source, task = target.get("source"), target.get("task")
        if source not in START_SOURCES or not task:
            continue
        near = [(at - e["t"], index) for index, at, subject in starts
                if subject == task and -JUST_AFTER <= at - e["t"] <= START_WINDOW]
        if not near:
            continue
        # The start that came next after the typing; failing that, the one that began just before it was recorded.
        gap, index = min(near, key=lambda pair: (pair[0] < 0, abs(pair[0])))
        record = events[index]["fields"]
        if source in typed.get(index, {}):
            alone.add(index)
        elif record.get("outcome") == "done":
            typed.setdefault(index, {})[source] = e
            claimed.add(e.get("id"))
        elif e.get("outcome") == "failed" and source in ACCOUNTS_FOR.get(record.get("reason"), ()):
            claimed.add(e.get("id"))  # the failed start's own sentence says it
        else:
            alone.add(index)
    return typed, claimed, alone


def _effect_text(e: dict, undone: str, now: float) -> str:
    if e.get("class") == "session.type":
        return _typing_text(e, now)
    done, tried, doing, _ = EFFECTS.get(e.get("class"), ("", "", "", ""))
    if not done:
        return "Assistant made a change outside itself because of it."
    outcome = e.get("outcome")
    if outcome == "done":
        because = BECAUSE.get((e.get("authorized_by") or {}).get("kind"))
        text = done + (f", {because}." if because else ".")
        if e.get("verified") is False:
            text += " It couldn't be confirmed."
        return text + (f" This undid the earlier {undone}." if undone else "")
    if outcome == "failed":
        error = (e.get("error") or "")
        said = ERRORS.get(error) or (f"error {m.group(1)}" if (m := HTTP.fullmatch(error)) else "")
        return f"Tried to {tried} and it failed" + (f" ({said})." if said else ".")
    if outcome == "unknown" or abs(now - float(e.get("t") or 0)) >= IN_FLIGHT_SEC:  # as effects.unfinished: a time
        return f"Tried to {tried} but didn't hear back, so it may or may not have gone through."  # off by this much
    return f"{doing} is in progress."  # can't be trusted, so what became of it isn't known


def _effect_steps(effects: list, now: float, skip=()) -> list:
    classes = {e.get("id"): e.get("class") for e in effects}
    steps = []
    for e in effects:
        if e.get("id") in skip:
            continue
        undone = EFFECTS.get(classes.get(e.get("undoes")), ("", "", "", ""))[3]
        at = e.get("finished_t") or e.get("t")
        steps.append(_step(at, "effect", _effect_text(e, undone, now), "effects"))
    return steps


# --- what you said --------------------------------------------------------------------------------------------

def _iso_seconds(stamp) -> float | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _feedback_text(record: dict, by_id: dict) -> str | None:
    signal = record.get("signal")
    what = WHAT.get(record.get("kind"), "this")
    if signal == "thumbs_down":
        reason = SAID.get(record.get("reason"))
        if record.get("amends"):
            return "You said why: " + reason + "." if reason else None
        return f"You said {what} is wrong."
    if signal == "retract":
        return TOOK_BACK.get((by_id.get(record.get("amends")) or {}).get("signal"), "You took something back.")
    return FEEDBACK.get(signal)


def _claim(pool: list, at: float) -> bool:
    """Take the change in `pool` (a (start, finish) pair) that a tap at `at` belongs to, if there is one: one thing
    pressed, one change made. The tap is recorded after the change returns, so it falls between the change's start and
    its finish, give or take PAIR_SEC."""
    for pair in pool:
        if pair[0] - PAIR_SEC <= at <= pair[1] + PAIR_SEC:
            pool.remove(pair)
            return True
    return False


def _feedback_steps(records: list, made: list, effects: list) -> list:
    """What you said. A tap that the task or the ledger says again is dropped, but only one record for each task or
    change, so a second tap that made nothing is still shown."""
    by_id = {r.get("id"): r for r in records}
    pools = {"send": [(t, t) for t in made], "send_handsoff": [(t, t) for t in made]}
    for e in effects:
        signal = SIGNAL_OF_EFFECT.get(e.get("class"))
        if signal and isinstance(e.get("t"), (int, float)):
            end = e.get("finished_t")
            pools.setdefault(signal, []).append((float(e["t"]), float(end) if isinstance(end, (int, float)) else float(e["t"])))
    steps = []
    for record in records:
        signal, at = record.get("signal"), _iso_seconds(record.get("ts"))
        if at is None:
            continue
        if signal in pools and _claim(pools["send"] if signal.startswith("send") else pools[signal], at):
            continue
        text = _feedback_text(record, by_id)
        if text is not None:
            steps.append(_step(at, "feedback", text, "feedback"))
    return steps


# --- putting it together ---------------------------------------------------------------------------------------

def _repeat(step: dict, n: int, last: float, floor: bool) -> dict:
    times = f"{'at least ' if floor else ''}{n} times"
    text = step["text"]
    if text.startswith(STARTING):
        text = f"Assistant tried to start an agent {times} and couldn't" + text[len(STARTING):]
    else:
        text += f" This happened {times}."
    return {**step, "text": text, "count": n, "last": last}


def _collapse(steps: list, cuts=None) -> list:
    """Steps that say the same thing one after another are one step: the first time, how many, and the last time. Each
    record is cut at its own depth, so a run is judged against the cut of the record it came from: `cuts` is {record:
    the time of the oldest step a read of it that stopped at its limit got to}. A run that begins at or before that may
    have begun earlier, so its count is a floor."""
    cuts = cuts or {}
    out, run = [], []
    for step in steps + [None]:
        if run and (step is None or (step["kind"], step["text"]) != (run[0]["kind"], run[0]["text"])):
            floor = run[0]["store"] in cuts and run[0]["at"] <= cuts[run[0]["store"]]
            out.append(run[0] if len(run) == 1 else _repeat(run[0], len(run), run[-1]["at"], floor))
            run = []
        if step is not None:
            run.append(step)
    return [{k: v for k, v in step.items() if k != "store"} for step in out]


def steps_of(events=(), effects=(), feedback=(), first=None, now=None, source="", cuts=None) -> list:
    """The steps, oldest first, from telemetry records, ledger effects, feedback records and the time the row was first
    seen. A step with a time that can't be one (not a number, or more than a day ahead) is left out. Never raises on a
    record it doesn't know."""
    now = time.time() if now is None else now
    events = sorted([r for r in events if isinstance(r, dict) and isinstance(r.get("fields"), dict)
                     and isinstance(r.get("ts"), (int, float))], key=lambda r: r["ts"])
    effects = [e for e in effects if isinstance(e, dict)]
    feedback = [r for r in feedback if isinstance(r, dict)]
    typed, claimed, alone = _pair_typings(events, effects)
    steps = _event_steps(events, typed, alone) + _effect_steps(effects, now, claimed)
    made = [r["ts"] for r in events if r.get("kind") == "task" and r["fields"].get("state") == "open"
            and not r["fields"].get("prev_state") and r["fields"].get("origin") == "work-queue"]
    steps += _feedback_steps(feedback, made, effects)
    if first is not None:
        steps.append(_step(first, "first_seen", "Assistant first saw this" + (f", in {source}." if source else ".")))
    steps = [s for s in steps if math.isfinite(s["at"]) and 0 < s["at"] <= now + FUTURE_SEC]
    steps.sort(key=lambda s: s["at"])  # stable: records at one moment keep the order they were gathered in
    return _collapse(steps, cuts)


def answer(state: str, steps=(), more: int = 0, before: bool = False, message: str = "", floor: bool = False,
           complete: bool = True) -> dict:
    """The answer the page gets. `message` is a sentence for a state that has no steps to show; `more` is how many
    earlier steps aren't shown, a floor (`floor`) when the read stopped at its limit; `before` says the row was here
    before the record began; `complete` is False when part couldn't be read."""
    plural = "steps aren't" if more != 1 else "step isn't"
    left = (("At least " if floor else "") + f"{more} earlier {plural} shown here.") if more else (
        "Earlier steps may not be shown here." if floor else "")
    return {"state": state, "steps": list(steps), "more": more, "message": message,
            "left_out": left[:1].upper() + left[1:], "note": BEFORE_RECORDS if before else "",
            "footer": PARTIAL_FOOTER if not complete else CUT_FOOTER if floor else FOOTER}


def empty(state: str) -> dict:
    """An answer with nothing in it: "none" (no record for this yet) or "unreadable"."""
    return answer(state, message=UNREADABLE if state == "unreadable" else NONE_YET, complete=state != "unreadable")


def read(trace: str, first=None, now=None, row: bool = True) -> dict:
    """Everything recorded about the piece of work with this trace id, as an answer. `first` is when it was first seen
    (`work_queue.traces.first_seen`); with none, and `row` true, the answer says the row was here before the record
    began (`row` is False for a result with no row to speak of). Never raises."""
    try:
        return _read(trace, first, now, row)
    except Exception:  # noqa: BLE001 - reading never breaks the page; what could not be read is said so
        return empty("unreadable")


def _gather(trace: str) -> tuple:
    """(telemetry records, effects, feedback records, the cuts, whether any of the three couldn't be read). The cuts are
    {record: the time of the oldest step a read of that record got to}, for each record whose read stopped at its limit
    (a ledger step is timed by when the change finished, as `_effect_steps` times it)."""
    unreadable, events, effects, feedback = False, [], [], []
    try:
        from assistant import telemetry
        state, found = telemetry.read_trace(trace, EVENTS_READ)
        events = list(reversed(found))
        unreadable = state not in ("ok", "missing")
    except Exception:  # noqa: BLE001
        unreadable = True
    try:
        from assistant import effects as ledger
        if ledger.readable():
            effects = list(ledger.history(trace=trace, limit=EFFECTS_READ))
        else:
            unreadable = True
    except Exception:  # noqa: BLE001
        unreadable = True
    try:
        from assistant import feedback as said
        if said.readable():
            feedback = list(reversed(said.recent(trace=trace, limit=FEEDBACK_READ)))
        else:
            unreadable = True
    except Exception:  # noqa: BLE001
        unreadable = True
    cuts = {}
    if len(events) >= EVENTS_READ:
        cuts["events"] = round(events[0]["ts"], 3)
    if len(effects) >= EFFECTS_READ:
        cuts["effects"] = round(min(e.get("finished_t") or e.get("t") or 0 for e in effects), 3)
    if len(feedback) >= FEEDBACK_READ:  # cut whatever its oldest record's time looks like: 0 floors no run, but the footer says so
        cuts["feedback"] = round(_iso_seconds(feedback[0].get("ts")) or 0, 3)
    return events, effects, feedback, cuts, unreadable


def _read(trace: str, first, now, row: bool) -> dict:
    from assistant.pointers import is_trace_id
    if not is_trace_id(trace):
        return empty("none")
    events, effects, feedback, cuts, unreadable = _gather(trace)
    cut = bool(cuts)
    first = first if type(first) in (int, float) and first > 0 else None
    steps = steps_of(events, effects, feedback, first, now, _source(events, feedback), cuts)
    more = max(0, len(steps) - SHOWN)
    shown = steps[-SHOWN:]
    before = first is None and row
    if unreadable:
        return answer("unreadable", shown, more, before, UNREADABLE, cut, False)
    if not shown:
        return answer("none", (), 0, before, NOTHING_SINCE, cut)
    return answer("ok", shown, more, before, "", cut)
