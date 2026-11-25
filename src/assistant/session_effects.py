"""Typing into an agent session, through the effects ledger.

Every keystroke Assistant's own code sends to an agent session goes through this module: `begin()` before the
first one, a `Keys` (made from what `begin()` returned) to send, `Keys.finish()` after. Nothing else in `bin/`,
`src/`, `work_queue/` or `hooks/` may name the cmux calls that type (`tests/test_session_typing_fence.py` fails the
build if anything does). Agents act on what is typed with their permission prompts off, and typing can't be taken
back, so:

  * No record, no typing. `begin()` raises `Refused` when the intent can't be written, and the caller types nothing.
  * Every typing names what allowed it: a person's tap, a task whose owner turned on auto-start, or a standing
    setting that is on. Text somebody else wrote is never what allows typing.
  * The ledger holds the session, the task, and where the text came from (a fixed name such as `nudge:stranded`),
    never the text, a prompt path, a title or a Slack message.
  * The same thing typed into the same session more than three times in half an hour is refused (a loop), whatever
    allowed it, except the first use of a one-time tap (the owner's own Slack message). A task or a Slack message
    counts as its own thing.
  * A typing that went out and couldn't be confirmed, or never got a result, is `unknown` and is not repeated by
    Assistant on its own. How long that lasts depends on who allowed it, because cmux reuses session refs:
      - a task's instruction and a Slack message keep the ledger's own rule (not again without a newer
        authorization);
      - a nudge, `/cleanup`, a boot instruction, a `/clear` or a merge request waits thirty minutes, then may be
        tried again (and counts toward the loop limit);
      - the owner's own Slack message is typed once whatever was typed before it; everything else, however it was
        authorized (a script run by hand or by an agent, "run now", restore), is held the same way.
  * A `Keys` is bound to its intent: one per effect, only the session or terminal the intent names, and only while
    the intent has no result.

What this can't prove: that the text typed is the text the caller says it came from, that a `source` or
`authorization` a caller gives is true (`bin/cmux-send.py` trusts the flags it is run with), or that the terminal a
caller types into is the one in the named session. The record says what Assistant did and what it said allowed it.

Standard library plus `assistant.effects` and `assistant.pointers`; it loads on the system Python (3.9).
"""
from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, timezone

from assistant import effects, pointers

CLASS = "session.type"
LOOP_MAX = 3
LOOP_SEC = 1800
LOOP_SCAN = 500
# A session the record can describe. cmux numbers its refs from 1000010000 and counts up.
SESSION = re.compile(r"workspace:[0-9]{1,18}")
# These have no time of their own to be allowed "as of", so an unknown one holds the same source in the same
# session for half an hour only. That applies to every one of them however it was authorized: the merge script run
# by hand, `bin/cmux-send.py` run by an agent, "run now" and restore are held like the pulse's typing.
WINDOWED_SOURCES = ("nudge:stranded", "cleanup", "boot", "clear", "merge", "pulse", "cli", "restore", "run-now")

UNRECORDED = ("Assistant couldn't record what it types, so it typed nothing. "
              "Check that your disk isn't full and that the folder ~/.assistant/effects can be written to.")
LOOP = ("Assistant already typed this into {where} three times in the last half hour, so it is waiting. "
        "It tries again once the oldest of them is 30 minutes old.")
IN_PROGRESS = "Assistant may still be typing this into {where} (or stopped partway), so it waited."
DONE = "Assistant already typed this into {where}, so it didn't type it again."
UNKNOWN_SHORT = ("Assistant typed this into {where} earlier and couldn't tell if it arrived, so it won't type it "
                 "again by itself.")
UNKNOWN = UNKNOWN_SHORT + " Look at that session. If it isn't there, type it into the session yourself."
UNAUTHORIZED = "Assistant couldn't confirm what allowed this, so it typed nothing. Run it again from the place you meant."
INVALID = "Assistant didn't recognise the session this was meant for (it expects workspace:N), so it typed nothing."
LATER = ("This message is dated later than this Mac's clock says it is now, so Assistant is waiting before typing "
         "it. If this Mac's clock is wrong, fix it.")

_now = time.time  # a seam, so tests set the clock
_USED: set = set()  # effect ids that already have their one Keys


class Refused(Exception):
    """Nothing may be typed. `code` is one of unrecorded, loop, in-progress, done, unknown, unauthorized, invalid,
    later; `str(exc)` is a plain sentence a person can read. `later`, `loop`, `in-progress` and `unrecorded` pass by
    themselves, so a caller keeps what it was going to type for the next try; the rest won't."""

    def __init__(self, code: str, message: str, short: str | None = None):
        super().__init__(message)
        self.code = code
        self.short = short or message  # the sentence without a command to run, for a log line about a message


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def writable() -> bool:
    """Whether the ledger can really be written to: a small probe is written, flushed to disk and removed, which a
    full disk refuses. For a caller that would otherwise start something (a new workspace) before it finds out that
    typing into it is refused."""
    probe = effects.ledger_dir() / f".probe-{os.getpid()}"
    try:
        os.makedirs(str(effects.ledger_dir()), mode=0o700, exist_ok=True)
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"x" * 512)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(str(probe))
        os.close(os.open(str(effects.ledger_path()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
        return True
    except OSError:
        return False


def trace_of(item) -> str:
    """The trace id of the task a typing is for: the task's own, else the stand-in `todostore.trace_of` gives, so the
    typing and the task's own records share one trace. "" for no task."""
    if not isinstance(item, dict):
        return ""
    from assistant import todostore  # noqa: PLC0415
    return todostore.trace_of(item)


def _loop_count(target: dict, now: float) -> int:
    """Typings into this session from this source in the last half hour that did, may have, or may still go out:
    anything but a typing known to have failed. A task or a Slack message is its own thing and counts only against
    itself."""
    count = 0
    for found in effects.history(CLASS, limit=LOOP_SCAN):
        if found["t"] >= now - LOOP_SEC and found["outcome"] != "failed" and found["target"] == target:
            count += 1
    return count


def _held_by_unknown(target: dict, now: float) -> bool:
    """Whether the last typing of this source into this session may have landed within the last half hour."""
    last = effects.last(CLASS, target)
    if not last:
        return False
    if last["outcome"] == "unknown":
        return now - last["finished_t"] < LOOP_SEC
    return last["outcome"] is None and effects.IN_FLIGHT_SEC <= now - last["t"] < LOOP_SEC


def _tap_used(where: str, once_tap: str) -> bool:
    """Whether this tap has already been used to type into this session (or terminal)."""
    return any(found["authorized_by"]["kind"] == "tap" and found["authorized_by"]["ref"] == once_tap
               and where in (found["target"].get("session"), found["target"].get("surface"))
               for found in effects.history(CLASS, limit=LOOP_SCAN))


def begin(session: str, source: str, *, kind: str, ref: str, component: str, task: str = "", trace: str = "",
          slack_ts: str = "", surface: str = "", anchor: float | None = None, once_tap: str = "") -> str:
    """Record the intent to type into `session` (`workspace:N`) and return the effect id. Type only if this returns.

    `source` is where the text comes from, a fixed name. `kind` and `ref` are what allowed it (`tap`, `owner_flag`
    or `standing`, and a pointer: a task id, the name of a nudge, a route). `anchor` is the time that allowance was
    given, when it has one of its own (a Slack message's time): the ledger then refuses a second typing of the same
    thing without a newer allowance. `once_tap` names a one-time tap (see below). `surface` names a terminal for a session known only by that id (`session` is
    then empty). Raises `Refused`."""
    now = _now()
    target: dict = {"source": source}
    if session:
        target["session"] = session
    if surface:
        target["surface"] = surface
    if task:
        target["task"] = task
    if slack_ts:
        target["ts"] = slack_ts
    where = session or "that terminal"
    if session and not SESSION.fullmatch(session) or not (session or surface):
        raise Refused("invalid", INVALID)
    try:
        target = effects.clean_target(target)
    except ValueError:
        raise Refused("invalid", INVALID) from None
    if anchor is not None and anchor > now + effects.FUTURE_SEC:
        raise Refused("later", LATER)
    # One thing is exempt from the loop limit and from the hold after an unknown: the first use of a tap that can
    # only be used once (`once_tap`: the owner's own Slack message, `slack:<time>`, after the owner rule has passed;
    # only Assistant's Slack path passes it, so no command line or route can claim it). Its second and later uses
    # (every Enter-only retry of that message) are counted and held. Everything else is counted and held, whatever
    # its authorization kind: a script can't tell a person from an agent, and a person can type into the session.
    held = not (kind == "tap" and once_tap and not _tap_used(session or surface, once_tap))
    if held and _loop_count(target, now) >= LOOP_MAX:
        raise Refused("loop", LOOP.format(where=where))
    if held and anchor is None and source in WINDOWED_SOURCES and _held_by_unknown(target, now):
        raise Refused("unknown", UNKNOWN.format(where=where), UNKNOWN_SHORT.format(where=where))
    # Recorded to the second, and rounded up when it is "now": rounded down it could fall before the result of a
    # typing that finished a moment ago, and the ledger would take it for an approval older than that result.
    auth = {"kind": kind, "ref": ref, "at": _iso(math.ceil(now) if anchor is None else anchor)}
    try:
        return effects.begin(CLASS, target, auth, trace=trace if pointers.is_trace_id(trace) else "",
                             component=component)
    except effects.Duplicate as exc:
        if exc.done:
            raise Refused("done", DONE.format(where=where)) from None
        if exc.unknown:
            raise Refused("unknown", UNKNOWN.format(where=where), UNKNOWN_SHORT.format(where=where)) from None
        raise Refused("in-progress", IN_PROGRESS.format(where=where)) from None
    except effects.NotAuthorized:
        raise Refused("unauthorized", UNAUTHORIZED) from None
    except effects.Unrecorded:
        raise Refused("unrecorded", UNRECORDED) from None
    except ValueError:
        raise Refused("invalid", INVALID) from None


def finish(effect_id: str, outcome: str, verified: bool | None = None, error: str = "") -> None:
    """Record how the typing ended: `done`, `failed` (nothing was sent) or `unknown` (the text may be there)."""
    effects.finish(effect_id, outcome, verified=verified, error=error)


# --- the only place the typing calls are named ------------------------------------------------------------------

class Keys:
    """The keystrokes of one typing, and the only way to send any. It is made from the effect id `begin()` returned,
    for the session (or terminal) that intent names: it is refused for anything else, there is one `Keys` for each
    effect, and each keystroke first checks that the intent has no result yet. `rpc(method, params)` answers with the
    reply, or None when cmux refused; it raises `TimeoutError` when it can't tell (the call took too long). Keystrokes are counted so the
    outcome can be told honestly; an exception from the transport counts as "can't tell" and goes on up."""

    def __init__(self, effect_id: str, rpc, surface_id: str, session: str = "", also=()):
        found = effects.get(effect_id) if isinstance(effect_id, str) else None
        if not found or found["class"] != CLASS or found["outcome"] is not None or effect_id in _USED:
            raise Refused("unrecorded", UNRECORDED)
        named = found["target"]
        # A terminal named in the intent is the only one; a session named in it is the one the caller says it types
        # into. Which terminal is in which session is cmux's to say, not something this module can check.
        if ("surface" in named and named["surface"] != surface_id) or \
                ("session" in named and named["session"] != session):
            raise Refused("invalid", INVALID)
        _USED.add(effect_id)
        self.effect_id, self.rpc, self.surface_id = effect_id, rpc, surface_id
        self.also = list(also)  # other intents for what is typed in the same turn; they get the same result
        self.sent = 0       # keystroke calls cmux accepted
        self.refused = 0    # keystroke calls cmux refused
        self.raised = False
        self.confirmed = None  # True/False once something has checked that it landed
        self.finished = False

    def _go(self, method: str, params: dict) -> bool:
        found = effects.get(self.effect_id)
        if not found or found["outcome"] is not None:
            raise Refused("done", DONE.format(where="the session"))
        try:
            ok = self.rpc(method, {"surface_id": self.surface_id, **params}) is not None
        except TimeoutError:
            self.raised = True  # it may have landed: that is "unknown", and the keystroke counts as not accepted
            return False
        except Exception:
            self.raised = True
            raise
        self.sent += ok
        self.refused += not ok
        return ok

    def text(self, text: str) -> bool:
        return self._go("surface.send_text", {"text": text})

    def key(self, key: str) -> bool:
        return self._go("surface.send_key", {"key": key})

    def carriage_return(self) -> bool:
        """Enter as a character, for a terminal that ignores the key."""
        return self.text("\r")

    def line(self, text: str, enter: str = "key", pause: float = 0.0, sleep=None) -> None:
        """Type `text`, then Enter (`enter` is "key", "cr" or "" for none). A transport error is counted, not raised."""
        try:
            if self.text(text) and enter:
                if pause:
                    (sleep or time.sleep)(pause)
                self.key("enter") if enter == "key" else self.carriage_return()
        except Refused:
            raise
        except Exception:
            pass

    def outcome(self) -> str:
        """`done` when it was seen to land, or every keystroke went out and nothing said it hadn't landed; `failed`
        when nothing went out; else `unknown`."""
        if self.confirmed:
            return "done"
        if self.raised or (self.sent and (self.refused or self.confirmed is False)):
            return "unknown"
        return "done" if self.sent else "failed"

    def finish(self) -> None:
        """Record how it ended, once."""
        if self.finished:
            return
        self.finished = True
        outcome = self.outcome()
        error = "" if outcome == "done" else "timeout" if self.raised else "tool failed" if self.refused else \
            "not confirmed" if outcome == "unknown" else ""
        for effect_id in [self.effect_id, *self.also]:
            finish(effect_id, outcome, verified=self.confirmed, error=error)


def type_line(effect_id: str, rpc, surface_id: str, text: str, *, session: str = "", enter: str = "key",
              pause: float = 0.0, sleep=None) -> str:
    """Type `text` and press Enter, then record how it ended and return the outcome. `effect_id` is what `begin()`
    returned. If it can't be used, the intent is closed as failed (nothing was typed)."""
    try:
        keys = Keys(effect_id, rpc, surface_id, session)
    except Refused:
        found = effects.get(effect_id) if isinstance(effect_id, str) else None
        if found and found["outcome"] is None:
            finish(effect_id, "failed", error="not recorded")
        raise
    try:
        keys.line(text, enter, pause, sleep)
    finally:
        keys.finish()
    return keys.outcome()
