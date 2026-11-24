"""Why is this here? The steps one piece of work went through, in plain sentences, from the records Assistant keeps.

Three records speak about a piece of work, all filed under its trace id: the telemetry record (`telemetry.py`: what the
model made of it, the task it became, the agent started on it, the checks after), the effects ledger (`effects.py`: every
change Assistant made outside itself because of it) and the feedback record (`feedback.py`: what you said about it).
`read` gathers them and `steps_of` turns them into sentences, oldest first. This is the one place those sentences are
made: the Work queue server only calls it, and the page only shows what it gets (and formats the times).

What it says is built from fixed words, ids, states and times. A title, a sender, a message, a link, a path, the model's
reason or the words a person typed is never read from a record, so it can't be copied into an answer. A name that a
record holds (a model, a check) is shown only when it is made of the characters a name has.

The answer is `{"state", "steps", "more", "message", "left_out", "note", "footer"}`. `state` is "ok", "none" (nothing is
recorded for it) or "unreadable" (a record couldn't be read, so what is shown may be missing something: it is never
"nothing recorded"). A record of a kind or a value this module doesn't know is never an error: it is left out and
counted in `more`, or given a general sentence where that reads true.

Standard library only at the top: each record is imported where it is read, so one that can't be loaded makes the answer
"unreadable" and nothing worse. Reading never writes, and every read is bounded.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

SHOWN = 50  # the newest steps shown; the count of the rest is `more`
EVENTS_READ = 400
EFFECTS_READ = 100
FEEDBACK_READ = 100

FOOTER = "This is everything Assistant has recorded about this item. The record stays on this Mac."
NONE_YET = "Assistant has no record for this yet. It starts one at the next refresh."
NOTHING_SINCE = "Nothing has been recorded about this since Assistant started keeping this record."
UNREADABLE = "Assistant can't read its record right now. Nothing is lost; try again in a minute."
BEFORE_RECORDS = ("This row was already here when Assistant started keeping this record, so earlier steps "
                  "aren't shown.")

LANES = {"do_first": "Do first", "promise": "Promise", "week": "This week", "optional": "Optional"}
SUGGESTS = {"you": "suggested you do it yourself", "agent": "said an agent can finish it",
            "handoff": "suggested you hand it off", "drop": "suggested you drop it"}
UNSORTED = {"call_failed": "the model's answer didn't come back",
            "bad_answer": "the model's answer couldn't be used",
            "not_answered": "the model left this one out of its answer",
            "out_of_time": "the sorting run ran out of time",
            "spend_limit": "today's spend limit for sorting was reached",
            "no_model": "no model is set up for sorting"}
DISPATCH_FAILED = {"session_tool_down": "the program that runs agent sessions didn't answer",
                   "no_workspace": "it couldn't open a workspace for it",
                   "no_surface": "the new workspace had no window to type in",
                   "session_not_ready": "the agent session wasn't ready for it",
                   "todo_not_marked": "it couldn't mark the task as started",
                   "other": "something went wrong"}
TASK_SAYS = {"done": "The agent reported it finished.", "blocked": "The agent reported it is blocked.",
             "removed": "The task was removed.", "deferred": "The task was put off."}
CHECKS = {"pr-merged": "the pull request is merged",
          "pr-closed": "the pull request is merged or closed",
          "you-commented": "you commented on or reviewed the pull request after the agent finished",
          "you-replied": "you replied in its Slack thread after the agent finished",
          "unblocked": "what the agent was blocked on looks cleared"}
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
            "tell": "You told Assistant something about it.", "retract": "You took that back."}
# What a feedback record is about, when it is not the row itself.
WHAT = {"draft": "this draft", "result": "this result"}
EFFECT_OF_SIGNAL = {"archive": "gmail.archive", "mark_read": "gmail.mark_read", "undo": "gmail.unarchive"}
HTTP = re.compile(r"http ([1-5][0-9]{2})")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}")
TASK_ID = re.compile(r"td-[0-9]{1,9}")
WORKSPACE = re.compile(r"workspace:[0-9]{1,18}")
IN_FLIGHT_SEC = 600  # as in effects.py: an effect with no result for this long is no longer "in progress"


def _name(value) -> str:
    """A name a record holds (a model, a rules version, a check), or "" when it isn't one."""
    return value if isinstance(value, str) and NAME.fullmatch(value) else ""


def _step(at, kind: str, text: str) -> dict:
    return {"at": round(float(at), 3), "kind": kind, "text": text}


# --- one telemetry record --------------------------------------------------------------------------------------

def _verdict(fields: dict) -> str:
    if fields.get("verdict") == "unsorted":
        why = UNSORTED.get(fields.get("reason"))
        return "The model didn't sort it" + (f": {why}." if why else ".")
    lane, says = LANES.get(fields.get("lane")), SUGGESTS.get(fields.get("verdict"))
    text = "The model " + (f"sorted it into {lane}" if lane else "looked at it")
    text += f" and {says}." if says else "."
    model, rules = _name(fields.get("model")), _name(fields.get("prompt_version"))
    given = ([f"Model: {model}"] if model else []) + ([f"rules: {rules}"] if rules else [])
    return text + (" " + ", ".join(given) + "." if given else "")


def _task(record: dict) -> str | None:
    fields, task = record["fields"], record["subject"] if TASK_ID.fullmatch(record["subject"] or "") else ""
    state = fields.get("state")
    if state == "open" and not fields.get("prev_state"):
        if fields.get("origin") != "work-queue":
            return "Assistant made " + (f"task {task}" if task else "a task") + " for it."
        how = ", hands-off" if fields.get("mode") == "handsoff" else " for a first pass"
        return f"You sent it to an agent{how}." + (f" It became task {task}." if task else "")
    if state == "open":
        return "The task was queued to start again."
    return TASK_SAYS.get(state)


def _dispatch(fields: dict) -> str | None:
    if fields.get("outcome") == "done":
        where = fields.get("workspace")
        text = "Assistant started an agent on it" + (f" in {where}." if WORKSPACE.fullmatch(where or "") else ".")
        return text + (" It couldn't confirm the session got the instruction." if fields.get("confirmed") is False else "")
    if fields.get("outcome") == "failed":
        why = DISPATCH_FAILED.get(fields.get("reason"))
        return "Assistant tried to start an agent and couldn't" + (f": {why}." if why else ".") \
            + " It tries again on the next round."
    return None


def _check(fields: dict) -> str | None:
    said = CHECKS.get(fields.get("name")) or _name(fields.get("name"))
    return f"Assistant checked the result: {said}." if said else None


EVENT_TEXT = {"triage_verdict": ("sorted", lambda r: _verdict(r["fields"])), "task": ("task", _task),
              "dispatch": ("started", lambda r: _dispatch(r["fields"])),
              "check": ("check", lambda r: _check(r["fields"]))}
# The telemetry copies of what the other two records hold: the ledger and the feedback record are the ones read.
COPIES = ("effect", "feedback")


def _event_steps(events: list) -> tuple:
    """(steps, how many records had no sentence). The kind word of a failed start is `start_failed`."""
    steps, skipped = [], 0
    for record in events:
        kind = record.get("kind")
        if kind in COPIES:
            continue
        kind_word, make = EVENT_TEXT.get(kind, (None, None))
        text = make(record) if make else None
        if kind == "task" and record["fields"].get("state") == "in-progress":
            continue  # right after a start: the start already says it
        if text is None:
            skipped += 1
            continue
        if kind == "dispatch" and record["fields"].get("outcome") == "failed":
            kind_word = "start_failed"
        elif kind == "triage_verdict" and record["fields"].get("verdict") == "unsorted":
            kind_word = "unsorted"
        steps.append(_step(record["ts"], kind_word, text))
    return steps, skipped


# --- the ledger -----------------------------------------------------------------------------------------------

def _effect_text(e: dict, undone: str, now: float) -> str:
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
    if outcome == "unknown" or now - float(e.get("t") or 0) >= IN_FLIGHT_SEC:
        return f"Tried to {tried} but didn't hear back, so it may or may not have gone through."
    return f"{doing} is in progress."


def _effect_steps(effects: list, now: float) -> list:
    classes = {e.get("id"): e.get("class") for e in effects}
    steps = []
    for e in effects:
        undone = EFFECTS.get(classes.get(e.get("undoes")), ("", "", "", ""))[3]
        at = e.get("finished_t") or e.get("t")
        steps.append(_step(at, "effect", _effect_text(e, undone, now)))
    return steps


# --- what you said --------------------------------------------------------------------------------------------

def _iso_seconds(stamp) -> float | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _feedback_text(record: dict) -> str | None:
    signal = record.get("signal")
    what = WHAT.get(record.get("kind"), "this")
    if signal == "thumbs_down":
        reason = SAID.get(record.get("reason"))
        if record.get("amends"):
            return "You said why: " + reason + "." if reason else None
        return f"You said {what} is wrong."
    return FEEDBACK.get(signal)


def _feedback_steps(records: list, sent: bool, classes: set) -> tuple:
    steps, skipped = [], 0
    for record in records:
        signal = record.get("signal")
        at = _iso_seconds(record.get("ts"))
        if (sent and signal in ("send", "send_handsoff")) or EFFECT_OF_SIGNAL.get(signal) in classes:
            continue  # the task, or the change in the ledger, says it already
        text = _feedback_text(record)
        if text is None or at is None:
            skipped += 1
            continue
        steps.append(_step(at, "feedback", text))
    return steps, skipped


# --- putting it together ---------------------------------------------------------------------------------------

def steps_of(events=(), effects=(), feedback=(), first=None, now=None) -> tuple:
    """(steps oldest first, how many records had no sentence) from telemetry records, ledger effects, feedback records
    and the time the row was first seen. Never raises on a record it doesn't know."""
    now = time.time() if now is None else now
    steps, skipped = _event_steps([r for r in events if isinstance(r, dict) and isinstance(r.get("fields"), dict)])
    steps += _effect_steps([e for e in effects if isinstance(e, dict)], now)
    sent = any(s["kind"] == "task" and s["text"].startswith("You sent it") for s in steps)
    more, extra = _feedback_steps([r for r in feedback if isinstance(r, dict)], sent,
                                  {e.get("class") for e in effects if isinstance(e, dict)})
    steps += more
    if first is not None:
        steps.append(_step(first, "first_seen", "Assistant first saw this."))
    steps.sort(key=lambda s: s["at"])  # stable: records at one moment keep the order they were gathered in
    return steps, skipped + extra


def answer(state: str, steps=(), more: int = 0, first_known: bool = True, message: str = "") -> dict:
    """The answer the page gets. `message` is a sentence for a state that has no steps to show."""
    return {"state": state, "steps": list(steps), "more": more,
            "message": message, "left_out": f"{more} more steps aren't shown here." if more else "",
            "note": "" if first_known else BEFORE_RECORDS, "footer": FOOTER}


def empty(state: str) -> dict:
    """An answer with nothing in it: "none" (no record for this yet) or "unreadable"."""
    return answer(state, message=UNREADABLE if state == "unreadable" else NONE_YET)


def read(trace: str, first=None, now=None) -> dict:
    """Everything recorded about the piece of work with this trace id, as an answer. `first` is when it was first seen
    (`work_queue.traces.first_seen`), or None for a row that was here before that was kept. Never raises."""
    try:
        return _read(trace, first, now)
    except Exception:  # noqa: BLE001 - reading never breaks the page; what could not be read is said so
        return empty("unreadable")


def _gather(trace: str) -> tuple:
    """(telemetry records, effects, feedback records, whether any of the three couldn't be read)."""
    unreadable, events, effects, feedback = False, [], [], []
    try:
        from assistant import telemetry
        state = telemetry.store_state()
        if state == "ok":
            events = list(reversed(telemetry.events(trace=trace, limit=EVENTS_READ, newest_first=True)))
        unreadable = state not in ("ok", "missing")
    except Exception:  # noqa: BLE001
        unreadable = True
    try:
        from assistant import effects as ledger
        effects = list(ledger.history(trace=trace, limit=EFFECTS_READ))
    except Exception:  # noqa: BLE001
        unreadable = True
    try:
        from assistant import feedback as said
        feedback = list(reversed(said.recent(trace=trace, limit=FEEDBACK_READ)))
    except Exception:  # noqa: BLE001
        unreadable = True
    return events, effects, feedback, unreadable


def _read(trace: str, first, now) -> dict:
    from assistant.pointers import is_trace_id
    if not is_trace_id(trace):
        return empty("none")
    events, effects, feedback, unreadable = _gather(trace)
    first = first if type(first) in (int, float) and first > 0 else None
    steps, skipped = steps_of(events, effects, feedback, first, now)
    more = max(0, len(steps) - SHOWN)
    shown = steps[-SHOWN:]
    left = more + skipped
    if unreadable:
        return answer("unreadable", shown, left, first is not None, UNREADABLE)
    if not shown:
        return answer("none", (), left, first is not None, NOTHING_SINCE)
    return answer("ok", shown, left, first is not None)
