"""Typing into an agent session, through the effects ledger.

Every keystroke Assistant's own code sends to an agent session goes through this module: `begin()` before the
first one, the `Keys` helper (or `send_text` / `send_key`) to send, `finish()` after. Nothing else in `bin/`, `src/`,
`work_queue/` or `hooks/` may name the cmux calls that type (`tests/test_session_typing_fence.py` fails the build if
anything does). Agents act on what is typed with their permission prompts off, and typing can't be taken back, so:

  * No record, no typing. `begin()` raises `Refused` when the intent can't be written, and the caller types nothing.
  * Every typing names what allowed it: a person's tap, a task whose owner turned on auto-start, or a standing
    setting that is on. Text somebody else wrote is never what allows typing.
  * The ledger holds the session, the task, and where the text came from (a fixed name such as `nudge:stranded`),
    never the text, a prompt path, a title or a Slack message.
  * The same thing typed into the same session more than three times in half an hour is refused (a loop), whatever
    else allows it. Only a Slack message or a task counts as a different thing from the one before it.
  * A typing whose outcome is unknown (the text went out and Enter, or the answer, didn't) is not repeated unless
    something newer allows it. A source with no natural time of its own (a nudge) counts as allowed "now", so the
    ledger's own rule would let it through; `begin()` refuses it explicitly.

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
_SESSION = re.compile(r"workspace:[0-9]{1,9}")

UNRECORDED = ("Assistant couldn't record what it types, so it typed nothing. "
              "Check that your disk isn't full and that the folder ~/.assistant/effects can be written to")
LOOP = "Assistant already typed this into the same session three times in the last half hour, so it didn't type it again"
IN_PROGRESS = "Assistant is already typing this into the same session"
DONE = "Assistant already typed this into the same session, so it didn't type it again"
UNKNOWN = ("Assistant typed this into the same session earlier and never saw it land, so it didn't type it again. "
           "Check the session")
UNAUTHORIZED = "Assistant couldn't confirm what allowed this, so it typed nothing"
INVALID = "Assistant couldn't describe where this was going, so it typed nothing"

_now = time.time  # a seam, so tests set the clock


class Refused(Exception):
    """Nothing may be typed. `code` is one of unrecorded, loop, in-progress, done, unknown, unauthorized, invalid;
    `str(exc)` is a plain sentence a person can read."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def writable() -> bool:
    """Whether the ledger can be written to at all, checked without writing a row. For a caller that would otherwise
    start something (a new workspace) before it finds out typing into it is refused."""
    try:
        os.makedirs(str(effects.ledger_dir()), mode=0o700, exist_ok=True)
        os.close(os.open(str(effects.ledger_path()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
        os.close(os.open(str(effects.ledger_dir() / "effects.lock"), os.O_RDWR | os.O_CREAT, 0o600))
        return True
    except OSError:
        return False


def trace_of(item) -> str:
    """The task's own trace id when it has a valid one."""
    found = item.get("trace") if isinstance(item, dict) else ""
    return found if pointers.is_trace_id(found) else ""


def _loop_count(target: dict, now: float) -> int:
    """Typings into this session from this source in the last half hour that did or may have gone out. A task or a
    Slack message is its own thing: it only counts against itself."""
    same = {k: target[k] for k in ("session", "surface", "source", "task", "ts") if k in target}
    count = 0
    for found in effects.history(CLASS, limit=LOOP_SCAN):
        if found["t"] < now - LOOP_SEC:
            break
        theirs = found["target"]
        if found["outcome"] != "failed" and all(theirs.get(k) == v for k, v in same.items()) \
                and all(k in same for k in theirs):
            count += 1
    return count


def begin(session: str, source: str, *, kind: str, ref: str, component: str, task: str = "", trace: str = "",
          slack_ts: str = "", surface: str = "", anchor: float | None = None) -> str:
    """Record the intent to type into `session` (`workspace:N`) and return the effect id. Type only if this returns.

    `source` is where the text comes from, a fixed name. `kind` and `ref` are what allowed it (`tap`, `owner_flag`
    or `standing`, and a pointer: a task id, the name of a nudge, a route). `anchor` is the time that allowance was
    given, when it has one of its own (a Slack message's time): the ledger then refuses a second typing of the same
    thing without a newer allowance. `surface` names a terminal for a session known only by that id (`session` is
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
    if session and not _SESSION.fullmatch(session) or not (session or surface):
        raise Refused("invalid", INVALID)
    try:
        target = effects.clean_target(target)
    except ValueError:
        raise Refused("invalid", INVALID) from None
    if _loop_count(target, now) >= LOOP_MAX:
        raise Refused("loop", LOOP)
    if anchor is None:
        last = effects.last(CLASS, target)
        if last and last["outcome"] == "unknown":
            raise Refused("unknown", UNKNOWN)
    # Recorded to the second, and rounded up when it is "now": rounded down it could fall before the result of a
    # typing that finished a moment ago, and the ledger would take it for an approval older than that result.
    auth = {"kind": kind, "ref": ref, "at": _iso(math.ceil(now) if anchor is None else anchor)}
    try:
        return effects.begin(CLASS, target, auth, trace=trace if pointers.is_trace_id(trace) else "",
                             component=component)
    except effects.Duplicate as exc:
        raise Refused("done" if exc.done else "unknown" if exc.unknown else "in-progress",
                      DONE if exc.done else UNKNOWN if exc.unknown else IN_PROGRESS) from None
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

def _open(effect_id) -> bool:
    """Whether this is a typing whose intent is on disk and which has no result yet."""
    return isinstance(effect_id, str) and any(
        found["id"] == effect_id and found["outcome"] is None for found in effects.history(CLASS, limit=LOOP_SCAN))


class Keys:
    """The keystrokes of one typing, and the only way to send any. It is made from the effect id `begin()` returned,
    so nothing can be typed without an intent on disk first. `rpc(method, params)` answers with the reply, or None
    when cmux refused. Keystrokes are counted so the outcome can be told honestly; an exception from the transport
    counts as "can't tell" and goes on up."""

    def __init__(self, effect_id: str, rpc, surface_id: str):
        if not _open(effect_id):
            raise Refused("unrecorded", UNRECORDED)
        self.effect_id, self.rpc, self.surface_id = effect_id, rpc, surface_id
        self.sent = 0       # keystroke calls cmux accepted
        self.refused = 0    # keystroke calls cmux refused
        self.raised = False
        self.confirmed = None  # True/False once something has checked that it landed

    def _go(self, method: str, params: dict) -> bool:
        try:
            ok = self.rpc(method, {"surface_id": self.surface_id, **params}) is not None
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

    def line(self, text: str, enter: str = "key", pause: float = 0.0, sleep=time.sleep) -> None:
        """Type `text`, then Enter (`enter` is "key", "cr" or "" for none). A transport error is counted, not raised."""
        try:
            if self.text(text) and enter:
                if pause:
                    sleep(pause)
                self.key("enter") if enter == "key" else self.carriage_return()
        except Exception:
            pass

    def outcome(self) -> str:
        """`done` when it was seen to land or every keystroke went out, `failed` when nothing went out, else
        `unknown`."""
        if self.confirmed:
            return "done"
        if self.raised:
            return "unknown"
        if not self.sent:
            return "failed"
        return "done" if not self.refused else "unknown"

    def finish(self) -> None:
        """Record how it ended."""
        error = "timeout" if self.raised else "tool failed" if self.refused else ""
        finish(self.effect_id, self.outcome(), verified=self.confirmed, error="" if self.outcome() == "done" else error)


def type_line(effect_id: str, rpc, surface_id: str, text: str, *, enter: str = "key", pause: float = 0.0,
              sleep=time.sleep) -> str:
    """Type `text` and press Enter, then record how it ended and return the outcome. `effect_id` is what `begin()`
    returned."""
    keys = Keys(effect_id, rpc, surface_id)
    keys.line(text, enter, pause, sleep)
    keys.finish()
    return keys.outcome()
