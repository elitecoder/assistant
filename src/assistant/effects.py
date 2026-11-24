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
    pointer, a link's host name, a cmux workspace, a task id, a Slack channel and message time. Nothing else is
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
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assistant import pointers
from assistant.redact import redact

# An intent with no result blocks a repeat for this long; after it, the intent no longer counts.
IN_FLIGHT_SEC = 600
ERROR_MAX = 160
OUTCOMES = ("done", "failed")
# The only error texts a row may hold, besides `http NNN`.
ERRORS = ("timeout", "network", "tool failed", "bad answer", "not confirmed", "other")
KINDS = ("tap", "decision", "owner_flag", "standing")

_HTTP = re.compile(r"http [1-5][0-9]{2}")
_ID = r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}"
_EFFECT_ID = re.compile(r"ef-[0-9a-f]{16}")
_REF = re.compile(r"[A-Za-z0-9_:./-]{1,100}")
_COMPONENT = re.compile(r"[A-Za-z0-9_:./-]{0,64}")
# Bounded, linear patterns, like redact.py's.
TARGET_KEYS = {
    "account": re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}"),
    "thread": re.compile(_ID),
    "row": re.compile(r"row:[0-9a-f]{16}"),
    "host": re.compile(r"[a-z0-9.-]{1,253}"),
    "session": re.compile(r"workspace:[0-9]{1,9}"),
    "task": re.compile(r"td-[0-9]{1,9}"),
    "channel": re.compile(r"[CDG][A-Z0-9]{8,11}"),
    "ts": re.compile(r"[0-9]{10}\.[0-9]{6}"),
}
_MSG_ID = re.compile(_ID)
MSGS_MAX = 100


class NotAuthorized(Exception):
    """The class doesn't accept this kind of authorization, the authorization is malformed, or the undo is invalid."""


class Duplicate(Exception):
    """The same effect is already in progress (`done` False) or was already done and not re-approved (`done` True)."""

    def __init__(self, message: str, done: bool = False):
        super().__init__(message)
        self.done = done


class Unrecorded(Exception):
    """The intent couldn't be written to disk, so the caller must not act."""


@dataclass(frozen=True)
class EffectClass:
    reversible: bool
    undone_by: str
    accepts: tuple


CLASSES = {
    "gmail.archive": EffectClass(True, "gmail.unarchive", ("tap",)),
    "gmail.unarchive": EffectClass(True, "gmail.archive", ("tap",)),
    "gmail.mark_read": EffectClass(True, "", ("tap",)),
    "gmail.trash": EffectClass(True, "gmail.untrash", ("decision",)),
    "gmail.untrash": EffectClass(True, "gmail.trash", ("decision",)),
    "mail.unsubscribe": EffectClass(False, "", ("decision",)),
    "slack.save.complete": EffectClass(True, "slack.save.uncomplete", ("tap", "decision")),
    "slack.save.uncomplete": EffectClass(True, "slack.save.complete", ("decision",)),
    "session.type": EffectClass(False, "", ("tap", "owner_flag", "standing")),
}


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
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _read() -> dict:
    """Every effect in the file, by id, in file order: its intent merged with its first result. Corrupt lines are
    skipped. Never raises."""
    try:
        with open(str(ledger_path()), "rb") as f:
            data = f.read()
    except OSError:
        return {}
    found: dict = {}
    for raw in data.split(b"\n"):
        try:
            row = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        eid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(eid, str):
            continue
        if row.get("kind") == "intent":
            if isinstance(row.get("class"), str) and isinstance(row.get("target"), dict) \
                    and isinstance(row.get("auth"), dict) and eid not in found:
                found[eid] = {"id": eid, "class": row["class"], "target": row["target"], "authorized_by": row["auth"],
                              "trace": _text(row.get("trace")), "component": _text(row.get("component")),
                              "undoes": _text(row.get("undoes")), "at": _text(row.get("at")), "t": _number(row.get("t")),
                              "outcome": None, "verified": None, "error": "", "finished_at": "", "finished_t": 0.0}
        elif row.get("kind") == "result" and eid in found and found[eid]["outcome"] is None \
                and row.get("outcome") in OUTCOMES:
            found[eid].update(outcome=row["outcome"], error=_text(row.get("error")), finished_at=_text(row.get("at")),
                              finished_t=_number(row.get("t")),
                              verified=row["verified"] if isinstance(row.get("verified"), bool) else None)
    return found


def _canon(target: dict) -> str:
    return json.dumps(target, sort_keys=True, separators=(",", ":"))


def _young(effect: dict, now: float) -> bool:
    return effect["outcome"] is None and now - effect["t"] < IN_FLIGHT_SEC


def _undone(effects: dict, effect_id: str, now: float) -> bool:
    """Whether an effect that reverses this one has succeeded or is still in flight."""
    return any(e["undoes"] == effect_id and (e["outcome"] == "done" or _young(e, now)) for e in effects.values())


def _never_raises(default):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - the contract is that a read never raises
                return default()
        return inner
    return wrap


@_never_raises(list)
def history(effect_class=None, target=None, trace=None, limit=100) -> list:
    """Each effect as one dict with its intent and result merged, newest first. Filters match exactly."""
    key = _canon(clean_target(target)) if target is not None else None
    found = [e for e in reversed(list(_read().values()))
             if (effect_class is None or e["class"] == effect_class) and (trace is None or e["trace"] == trace)
             and (key is None or _canon(e["target"]) == key)]
    return found[:max(limit, 0)]


@_never_raises(lambda: None)
def last(effect_class, target):
    """The newest effect of this class on this target, or None."""
    found = history(effect_class, target, limit=1)
    return found[0] if found else None


@_never_raises(list)
def unfinished(older_than_sec=IN_FLIGHT_SEC) -> list:
    """Intents with no result that are at least this old: the actions whose outcome is unknown."""
    now = _now()
    return [e for e in _read().values() if e["outcome"] is None and now - e["t"] >= older_than_sec]


@_never_raises(str)
def latest_undoable(effect_class: str, target: dict) -> str:
    """The id of the newest successful effect on this target that `effect_class` undoes and nothing has undone yet,
    or "". Pass it as `undoes` when a person reverses something the ledger recorded."""
    key, effects, now = _canon(clean_target(target)), _read(), _now()
    for e in reversed(list(effects.values())):
        spec = CLASSES.get(e["class"])
        if spec and spec.undone_by == effect_class and e["outcome"] == "done" and _canon(e["target"]) == key \
                and not _undone(effects, e["id"], now):
            return e["id"]
    return ""


# --- writing --------------------------------------------------------------------------------------------------

def clean_target(target) -> dict:
    """The target, checked against the allow-list. ValueError for anything that isn't a pointer."""
    if not isinstance(target, dict) or not target or len(target) > len(TARGET_KEYS) + 1:
        raise ValueError("a target is a small dict of pointers")
    clean: dict = {}
    for key, value in target.items():
        if key == "msgs":
            if not isinstance(value, (list, tuple)) or not 0 < len(value) <= MSGS_MAX or not all(
                    isinstance(v, str) and _MSG_ID.fullmatch(v) for v in value):
                raise ValueError("msgs must be a short list of message ids")
            clean[key] = sorted(set(value))
        elif key in TARGET_KEYS and isinstance(value, str) and TARGET_KEYS[key].fullmatch(value):
            clean[key] = value
        else:
            raise ValueError(f"a target can't hold {key if key in TARGET_KEYS or key == 'msgs' else 'that key'} like this")
    return clean


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


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


def begin(effect_class, target, authorized_by, trace="", component="", undoes="") -> str:
    """Record the intent to do an effect and return its id. The caller acts only if this returns.

    ValueError: an unknown class, or a target, trace, component or `undoes` that isn't a pointer.
    NotAuthorized: this kind of authorization isn't accepted for the class, or `undoes` isn't a valid undo.
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
        with _locked():
            effects, now = _read(), _now()
            same = [e for e in effects.values() if e["class"] == effect_class and _canon(e["target"]) == key]
            if any(_young(e, now) for e in same):
                raise Duplicate("the same action is already in progress")
            if not spec.reversible and any(e["outcome"] == "done" and given <= e["finished_t"] for e in same):
                raise Duplicate("this can't be undone and was already done", done=True)
            if undoes:
                prior = effects.get(undoes)
                before = CLASSES.get(prior["class"]) if prior else None
                if not (prior and before and prior["outcome"] == "done" and before.reversible
                        and before.undone_by == effect_class and _canon(prior["target"]) == key):
                    raise NotAuthorized("that isn't an effect this one can undo")
                if _undone(effects, undoes, now):
                    raise NotAuthorized("that effect was already undone")
            effect_id = "ef-" + secrets.token_hex(8)
            _write_row({"v": 1, "kind": "intent", "id": effect_id, "at": _iso(now), "t": round(now, 3),
                        "class": effect_class, "target": target, "auth": auth, "trace": trace,
                        "component": component, "undoes": undoes})
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
            raise ValueError("outcome must be done or failed")
        with _locked():
            found = _read().get(effect_id)
            if found is None or found["outcome"] is not None:
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
