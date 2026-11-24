"""The effects ledger: one append-only record of everything Assistant changes outside itself.

An effect is a change outside Assistant: archiving a mail, unsubscribing, marking a Slack save done, typing into an
agent session. Each one writes an INTENT row before it acts and a RESULT row after, in
~/.assistant/effects/effects.jsonl (folder 0700, file 0600, one JSON object per line).

The ledger is authoritative, not telemetry, so its rules are checks, not hopes:

  * No record, no action. `begin()` raises `Unrecorded` when the intent can't be written and fsynced; the caller
    then does not act.
  * An effect that can't be undone needs the right kind of approval first. `CLASSES` says, in code, whether each
    class is reversible and which kinds of authorization it accepts; `begin()` refuses anything else. There is no
    configuration file: nothing can loosen this table.
  * Append-only. The ledger file is opened for appending and nowhere else; no function rewrites or deletes a row.
    A result is a second row, never an edit of the intent.
  * Pointers, not content. A row holds ids. What a target may hold is the allow-list `TARGET_KEYS` (and `msgs`), and
    each value must match its pattern: the person's own Gmail address (needed to undo), a thread or message id, a row
    pointer, a link's host name, a cmux workspace, a task id, a Slack channel and message time, where typed text came from (a fixed name) and a
    terminal's id. Nothing else is
    accepted, so a subject, a sender's name, message text or an unsubscribe link (which is itself a credential)
    can't be written. Every string also passes through `assistant.redact` on its way to the file.
  * `error` is a class of error, never a server's message: one of `ERRORS`, or `http NNN`. Anything else is
    recorded as `other`.

Every read-check-append runs under one flock on a dedicated lock file, because the Work queue server is threaded and
the pulse is another process. Paths are computed per call so tests that move HOME see fresh ones. A corrupt line is
skipped when reading and never stops a read or a write; reads never raise. The file is never trimmed.

Standard library plus `assistant.redact` and `assistant.pointers`; it loads on the system Python (3.9).
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assistant import pointers
from assistant.redact import redact

# An intent with no result blocks a repeat for this long; after it, the intent no longer counts.
IN_FLIGHT_SEC = 600
# An approval dated further ahead than this is refused: it would outrank every result and switch off repeat protection.
FUTURE_SEC = 60
ERROR_MAX = 160
READ_BLOCK = 4 * 1024 * 1024
# How much of the file just before the read position is remembered, to notice the file was rewritten in place.
TAIL_BYTES = 64
# `unknown`: the action may or may not have happened (it was sent and no answer came back).
OUTCOMES = ("done", "failed", "unknown")
# The only error texts a row may hold, besides `http NNN`.
ERRORS = ("timeout", "network", "tool failed", "bad answer", "not confirmed", "not recorded", "other")
KINDS = ("tap", "decision", "owner_flag", "standing")

_HTTP = re.compile(r"http [1-5][0-9]{2}")
_ID = r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}"
_EFFECT_ID = re.compile(r"ef-[0-9a-f]{16}")
_REF = re.compile(r"[A-Za-z0-9_:./-]{1,100}")
_COMPONENT = re.compile(r"[A-Za-z0-9_:./-]{0,64}")
# Bounded, linear patterns, like redact.py's.
TARGET_KEYS = {
    # The characters an address's local part may hold, the apostrophe among them. The caller has already matched the
    # address to a linked account.
    "account": re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,255}"),
    "thread": re.compile(_ID),
    "row": re.compile(r"row:[0-9a-f]{16}"),
    # `<redacted>` is allowed so a host already scrubbed is still a valid target (cleaning twice changes nothing).
    "host": re.compile(r"[a-z0-9._:<>-]{1,253}"),
    "msg_count": re.compile(r"[0-9]{1,9}"),
    "msg_digest": re.compile(r"[0-9a-f]{16}"),
    "session": re.compile(r"workspace:[0-9]{1,9}"),
    "task": re.compile(r"td-[0-9]{1,9}"),
    "channel": re.compile(r"[CDG][A-Z0-9]{8,11}"),
    "ts": re.compile(r"[0-9]{10}\.[0-9]{6}"),
    # Where typed text came from, as a short fixed name (`task`, `nudge:stranded`, `slack-message`), never the text.
    "source": re.compile(r"[a-z][a-z-]{0,23}(?::[a-z][a-z-]{0,23})?"),
    # A terminal's id, for a session the saved cmux state knows only by that id.
    "surface": re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"),
}
_MSG_ID = re.compile(_ID)
MSGS_MAX = 100


class NotAuthorized(Exception):
    """The class doesn't accept this kind of authorization, the authorization is malformed, or the undo is invalid."""


class Duplicate(Exception):
    """The same effect is already in progress (neither flag), was already done and not re-approved (`done`), or was
    sent and never answered, so it may have happened, and wasn't re-approved (`unknown`)."""

    def __init__(self, message: str, done: bool = False, unknown: bool = False):
        super().__init__(message)
        self.done, self.unknown = done, unknown


class Unrecorded(Exception):
    """The intent couldn't be written to disk, so the caller must not act."""


@dataclass(frozen=True)
class EffectClass:
    reversible: bool
    undone_by: str
    accepts: tuple


CLASSES = types.MappingProxyType({
    "gmail.archive": EffectClass(True, "gmail.unarchive", ("tap",)),
    "gmail.unarchive": EffectClass(True, "gmail.archive", ("tap",)),
    "gmail.mark_read": EffectClass(True, "", ("tap",)),
    "gmail.trash": EffectClass(True, "gmail.untrash", ("decision",)),
    "gmail.untrash": EffectClass(True, "gmail.trash", ("decision",)),
    "mail.unsubscribe": EffectClass(False, "", ("decision",)),
    "slack.save.complete": EffectClass(True, "slack.save.uncomplete", ("tap", "decision")),
    "slack.save.uncomplete": EffectClass(True, "slack.save.complete", ("decision",)),
    "session.type": EffectClass(False, "", ("tap", "owner_flag", "standing")),
})


# --- paths and the lock ---------------------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def ledger_dir() -> Path:
    return _home() / ".assistant" / "effects"


def ledger_path() -> Path:
    return ledger_dir() / "effects.jsonl"


def _lock_path() -> Path:
    return ledger_dir() / "effects.lock"


def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Seams, so a test can make a write or a flush fail.
_write = os.write
_fsync = os.fsync


@contextlib.contextmanager
def _locked():
    os.makedirs(str(ledger_dir()), mode=0o700, exist_ok=True)
    os.chmod(str(ledger_dir()), 0o700)
    fd = os.open(str(_lock_path()), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _append(line: bytes) -> None:
    """Add one line to the end of the ledger and flush it to disk. The only place the ledger is opened for writing."""
    fd = os.open(str(ledger_path()), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line  # a crash left a torn last line: don't glue this row onto it
        view = memoryview(line)
        while view:
            view = view[_write(fd, view):]
        _fsync(fd)
    finally:
        os.close(fd)


def _scrub(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _write_row(row: dict) -> None:
    _append((json.dumps(_scrub(row), separators=(",", ":")) + "\n").encode("utf-8"))


# --- reading --------------------------------------------------------------------------------------------------

def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _number(value) -> float:
    """A finite number, or 0.0 for anything else (a bool, text, infinity, an integer too big for a float)."""
    try:
        found = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    except OverflowError:
        return 0.0
    return found if math.isfinite(found) else 0.0


def _canon(target: dict) -> str:
    return json.dumps(target, sort_keys=True, separators=(",", ":"))


class _Effect:
    """What the checks need to know about one effect. The rest of its rows stays in the file, where `where` points,
    and is read only when someone asks for the effect's history: a year of use is 100,000 of these."""

    __slots__ = ("id", "cls", "key", "t", "trace", "undoes", "outcome", "finished_t", "intent", "result")

    def __init__(self, row: dict, key: str, intent: tuple):
        self.id, self.cls, self.key = row["id"], row["class"], key
        self.t, self.trace, self.undoes = _number(row.get("t")), _text(row.get("trace")), _text(row.get("undoes"))
        self.outcome, self.finished_t, self.intent, self.result = None, 0.0, intent, None


class _State:
    """The ledger as read so far, in one process. The file is only ever appended to, so it is read once and then
    only the bytes added since: `offset` is the end of the last whole line read. The file's first bytes and its
    identity say whether it is still the file that was read; if not, it is read again from the start."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.ident, self.head, self.tail, self.offset = None, b"", b"", 0
        self.effects: dict = {}    # id -> _Effect, in file order
        self.by_target: dict = {}  # target -> effects of any class
        self.undo_of: dict = {}    # id -> effects that say they undo it
        self.open: dict = {}       # ids with no result yet
        self.unknown: dict = {}    # ids whose result says the outcome is unknown

    def ingest(self, raw: bytes, where: int) -> None:
        try:
            row = json.loads(raw)
        except (ValueError, RecursionError):
            return
        eid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(eid, str):
            return
        if row.get("kind") == "intent":
            if isinstance(row.get("class"), str) and isinstance(row.get("target"), dict) \
                    and isinstance(row.get("auth"), dict) and eid not in self.effects:
                key = _canon(row["target"])
                found = self.effects[eid] = _Effect(row, key, (where, len(raw)))
                self.by_target.setdefault(key, []).append(found)
                if found.undoes:
                    self.undo_of.setdefault(found.undoes, []).append(found)
                self.open[eid] = found
        elif row.get("kind") == "result" and eid in self.open and row.get("outcome") in OUTCOMES:
            found = self.open.pop(eid)
            found.outcome, found.finished_t, found.result = row["outcome"], _number(row.get("t")), (where, len(raw))
            if found.outcome == "unknown":
                self.unknown[eid] = found


_STATE = _State()
# Readers in other threads share the one state, so every use of it holds this.
_STATE_LOCK = threading.RLock()


def _sync() -> _State:
    """The state, brought up to date with the file. Call it holding _STATE_LOCK. A missing file is an empty ledger;
    any other failure to read raises OSError, so a caller that must not act on a guess (begin) doesn't."""
    state = _STATE
    try:
        fd = os.open(str(ledger_path()), os.O_RDONLY)
    except FileNotFoundError:
        state.reset()
        return state
    try:
        info = os.fstat(fd)
        ident = (info.st_dev, info.st_ino)
        if state.ident != ident or (state.offset and (
                os.pread(fd, len(state.head), 0) != state.head
                or os.pread(fd, len(state.tail), state.offset - len(state.tail)) != state.tail)):
            state.reset()  # another file, or this one shortened or rewritten in place
            state.ident = ident
        while info.st_size > state.offset:
            # In blocks, so a ledger of a year's use isn't held in memory twice while it is read.
            chunk = os.pread(fd, min(info.st_size - state.offset, READ_BLOCK), state.offset)
            cut = chunk.rfind(b"\n") + 1  # a line still being written isn't read until it is whole
            if not cut and state.offset + len(chunk) < info.st_size:
                chunk = os.pread(fd, info.st_size - state.offset, state.offset)  # one line longer than a block
                cut = chunk.rfind(b"\n") + 1
            if not cut:
                break
            if not state.offset:
                state.head = chunk[:min(64, cut)]
            where = state.offset
            for raw in chunk[:cut].split(b"\n"):
                state.ingest(raw, where)
                where += len(raw) + 1
            state.offset += cut
            state.tail = (state.tail + chunk[:cut])[-TAIL_BYTES:]
    finally:
        os.close(fd)
    return state


def _forget() -> None:
    """Drop what was read, so the next call reads the whole file. For tests that compare the two."""
    with _STATE_LOCK:
        _STATE.reset()


class _Stale(Exception):
    """A row the state points at isn't what the state says it is: the file was changed under it."""


def _row(fd: int, where, effect_id: str) -> dict:
    try:
        row = json.loads(os.pread(fd, where[1], where[0]))
        if row["id"] != effect_id:
            raise ValueError("another row")
    except (ValueError, KeyError, TypeError, RecursionError):
        raise _Stale() from None
    return row


def _publish(found: list) -> list:
    """The effects as dicts, each its intent merged with its result, read from the file."""
    fd = os.open(str(ledger_path()), os.O_RDONLY)
    try:
        out = []
        for e in found:
            intent = _row(fd, e.intent, e.id)
            result = _row(fd, e.result, e.id) if e.result else {}
            if result and result.get("outcome") != e.outcome:
                raise _Stale()
            out.append({"id": e.id, "class": e.cls, "target": intent["target"], "authorized_by": intent["auth"],
                        "trace": e.trace, "component": _text(intent.get("component")), "undoes": e.undoes,
                        "at": _text(intent.get("at")), "t": e.t, "outcome": e.outcome,
                        "verified": result["verified"] if isinstance(result.get("verified"), bool) else None,
                        "error": _text(result.get("error")), "finished_at": _text(result.get("at")),
                        "finished_t": e.finished_t})
        return out
    finally:
        os.close(fd)


def _young(effect: _Effect, now: float) -> bool:
    """An intent with no result whose time is within ten minutes of now, either way. One dated further off, or with
    no usable time, is an action whose outcome is unknown, not one in progress."""
    return effect.outcome is None and abs(now - effect.t) < IN_FLIGHT_SEC


def _undone(state: _State, effect_id: str, now: float) -> bool:
    """Whether an effect that reverses this one has succeeded or is still in flight."""
    return any(e.outcome == "done" or _young(e, now) for e in state.undo_of.get(effect_id, ()))


def _never_raises(default):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                with _STATE_LOCK:
                    try:
                        return fn(*args, **kwargs)
                    except _Stale:
                        _STATE.reset()  # the file changed under what was read: read it again from the start
                        return fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - the contract is that a read never raises
                return default()
        return inner
    return wrap


@_never_raises(list)
def history(effect_class=None, target=None, trace=None, limit=100) -> list:
    """Each effect as one dict with its intent and result merged, newest first. Filters match exactly."""
    state = _sync()
    pool = state.effects.values() if target is None else state.by_target.get(_canon(clean_target(target, True)), ())
    found = []
    for e in reversed(list(pool)):
        if len(found) >= limit:
            break
        if (effect_class is None or e.cls == effect_class) and (trace is None or e.trace == trace):
            found.append(e)
    return _publish(found)


@_never_raises(lambda: None)
def last(effect_class, target):
    """The newest effect of this class on this target, or None."""
    found = history(effect_class, target, limit=1)
    return found[0] if found else None


@_never_raises(list)
def unfinished(older_than_sec=IN_FLIGHT_SEC) -> list:
    """Intents with no result that are at least this old, or whose time can't be trusted: the actions whose outcome
    is unknown."""
    now = _now()
    return _publish([e for e in _sync().open.values() if now - e.t >= older_than_sec or e.t - now >= IN_FLIGHT_SEC])


@_never_raises(list)
def uncertain() -> list:
    """Effects that were sent and never answered: they may or may not have happened."""
    return _publish(list(_sync().unknown.values()))


@_never_raises(str)
def latest_undoable(effect_class: str, target: dict) -> str:
    """The id of the newest successful effect on this target that `effect_class` undoes and nothing has undone yet,
    or "". Pass it as `undoes` when a person reverses something the ledger recorded."""
    state, now = _sync(), _now()
    for e in reversed(state.by_target.get(_canon(clean_target(target, True)), [])):
        spec = CLASSES.get(e.cls)
        if spec and spec.undone_by == effect_class and e.outcome == "done" and not _undone(state, e.id, now):
            return e.id
    return ""


# --- writing --------------------------------------------------------------------------------------------------

def _host(value: str) -> str:
    """A host name as it is recorded: lower case, no trailing dot, in its ASCII (IDNA) form. One that still doesn't
    fit is recorded as a digest of itself, so the effect can be recorded and the same host is still the same target."""
    name = value.lower().rstrip(".")
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if TARGET_KEYS["host"].fullmatch(name):
        return name
    return "h:" + hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:16]


def _plain_id(value) -> str:
    """A thread or message id that scrubbing leaves alone. Real Gmail ids never look like a secret, and two ids that
    scrub to the same text would be one target, so one that changes is refused."""
    if not isinstance(value, str) or not _MSG_ID.fullmatch(value) or redact(value) != value:
        raise ValueError("an id must be a plain Gmail id")
    return value


def clean_target(target, stored=False) -> dict:
    """The target exactly as it is written to the file: checked against the allow-list, then scrubbed. Everything
    that compares targets compares this form, so two targets that scrub to the same text are the same target.
    A conversation with more than MSGS_MAX message ids is recorded as their count and a digest, and only this
    function sets those two keys: a target read back from the file (`stored`) may hold them, a caller's may not.
    ValueError for anything that isn't a pointer; the message never holds a value."""
    if not isinstance(target, dict) or not target or len(target) > len(TARGET_KEYS) + 1:
        raise ValueError("a target is a small dict of pointers")
    clean: dict = {}
    for key, value in target.items():
        if key == "msgs":
            if not isinstance(value, (list, tuple)) or not value:
                raise ValueError("msgs must be a list of message ids")
            ids = sorted(set(_plain_id(v) for v in value))
            if len(ids) > MSGS_MAX:
                clean["msg_count"] = str(len(ids))
                clean["msg_digest"] = hashlib.sha256(",".join(ids).encode("ascii")).hexdigest()[:16]
            else:
                clean[key] = ids
        elif key in ("msg_count", "msg_digest") and stored and "msgs" not in target \
                and isinstance(value, str) and TARGET_KEYS[key].fullmatch(value):
            clean[key] = value
        elif key == "thread":
            clean[key] = _plain_id(value)
        elif key == "host" and isinstance(value, str) and value:
            clean[key] = _host(value)
        elif key in TARGET_KEYS and key not in ("msg_count", "msg_digest") and isinstance(value, str) \
                and TARGET_KEYS[key].fullmatch(value):
            clean[key] = value
        else:
            raise ValueError(f"a target can't hold {key if key in TARGET_KEYS or key == 'msgs' else 'that key'} like this")
    return _scrub(clean)


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _authorization(spec: EffectClass, authorized_by) -> tuple:
    """(the authorization as it is recorded, when it was given as epoch seconds), or NotAuthorized."""
    if not isinstance(authorized_by, dict) or set(authorized_by) != {"kind", "ref", "at"}:
        raise NotAuthorized("the authorization must say its kind, ref and time")
    kind, ref = authorized_by["kind"], authorized_by["ref"]
    if kind not in spec.accepts:
        raise NotAuthorized("this kind of authorization isn't accepted for this action")
    given = _parse_time(authorized_by["at"])
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or given is None:
        raise NotAuthorized("the authorization's ref or time is malformed")
    return {"kind": kind, "ref": ref, "at": _iso(given)}, given


def _abandon(effect_id: str) -> None:
    """The intent may have landed although writing it failed, for an action that will never run: close it as failed,
    so a retry isn't held off. If this fails too, the caller's error is still what goes up and the intent ages out."""
    now = _now()
    _write_row({"v": 1, "kind": "result", "id": effect_id, "at": _iso(now), "t": round(now, 3),
                "outcome": "failed", "verified": None, "error": "not recorded"})


def begin(effect_class, target, authorized_by, trace="", component="", undoes="") -> str:
    """Record the intent to do an effect and return its id. The caller acts only if this returns.

    ValueError: an unknown class, or a target, trace, component or `undoes` that isn't a pointer.
    NotAuthorized: this kind of authorization isn't accepted for the class, it is malformed or dated more than a
    minute ahead, or `undoes` isn't a valid undo.
    Duplicate: the same class and target is in flight (an intent under ten minutes old with no result), or, for an
    effect that can't be undone, already succeeded and the authorization isn't newer than that.
    Unrecorded: the row couldn't be written and fsynced."""
    if not isinstance(effect_class, str) or effect_class not in CLASSES:
        raise ValueError("unknown effect class")
    spec = CLASSES[effect_class]
    target = clean_target(target)
    if trace and not pointers.is_trace_id(trace):
        raise ValueError("trace must be a trace id")
    if not isinstance(component, str) or not _COMPONENT.fullmatch(component):
        raise ValueError("component must be a short name")
    if undoes and not (isinstance(undoes, str) and _EFFECT_ID.fullmatch(undoes)):
        raise ValueError("undoes must be an effect id")
    auth, given = _authorization(spec, authorized_by)
    key = _canon(target)
    try:
        with _locked(), _STATE_LOCK:
            state, now = _sync(), _now()
            if given > now + FUTURE_SEC:
                raise NotAuthorized("the authorization is dated in the future")
            same = [e for e in state.by_target.get(key, ()) if e.cls == effect_class]
            if any(_young(e, now) for e in same):
                raise Duplicate("the same action is already in progress")
            # An approval dated a little ahead still only counts from now, so it can't outrank a result that is newer.
            counted = min(given, now)
            if not spec.reversible:
                if any(e.outcome == "done" and counted <= e.finished_t for e in same):
                    raise Duplicate("this can't be undone and was already done", done=True)
                if any(e.outcome == "unknown" and counted <= e.finished_t for e in same):
                    raise Duplicate("this can't be undone and may already have been done", unknown=True)
            if undoes:
                prior = state.effects.get(undoes)
                before = CLASSES.get(prior.cls) if prior else None
                if not (prior and before and prior.outcome == "done" and before.undone_by == effect_class
                        and prior.key == key):
                    raise NotAuthorized("that isn't an effect this one can undo")
                if _undone(state, undoes, now):
                    raise NotAuthorized("that effect was already undone")
            effect_id = "ef-" + secrets.token_hex(8)
            try:
                _write_row({"v": 1, "kind": "intent", "id": effect_id, "at": _iso(now), "t": round(now, 3),
                            "class": effect_class, "target": target, "auth": auth, "trace": trace,
                            "component": component, "undoes": undoes})
            except OSError:
                _abandon(effect_id)
                raise
    except OSError as exc:
        raise Unrecorded(f"couldn't record the intent ({type(exc).__name__})") from None
    return effect_id


def _error_class(error) -> str:
    text = redact(error if isinstance(error, str) else "")[:ERROR_MAX]
    if not text:
        return ""
    return text if text in ERRORS or _HTTP.fullmatch(text) else "other"


def finish(effect_id, outcome, verified=None, error="") -> bool:
    """Record how an effect ended. Never raises: when the row can't be written it prints one line to stderr and
    returns False. The action has already happened by then, and `unfinished()` will show it."""
    try:
        if outcome not in OUTCOMES:
            raise ValueError("outcome must be done, failed or unknown")
        with _locked(), _STATE_LOCK:
            found = _sync().effects.get(effect_id)
            if found is None or found.outcome is not None:
                raise ValueError("no open intent with that id")
            now = _now()
            _write_row({"v": 1, "kind": "result", "id": effect_id, "at": _iso(now), "t": round(now, 3),
                        "outcome": outcome, "verified": verified if isinstance(verified, bool) else None,
                        "error": _error_class(error)})
        return True
    except Exception as exc:  # noqa: BLE001 - finish runs after the action; it must not undo it by raising
        print(f"effects: couldn't record the result of {redact(str(effect_id))[:40]} ({type(exc).__name__})",
              file=sys.stderr)
        return False
