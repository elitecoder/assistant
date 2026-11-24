"""Telemetry: a local record of what Assistant did, as ids, counts, timings, states and money.

A record never holds content: no message text, title, prompt, model reason or summary, email address, file path
or link. That is enforced here, by the field registry `KINDS`, and not left to callers: a field a kind doesn't
list, or a value that isn't a number, a yes/no or a pointer (see `_pointer_ok`), is left out and counted in the
record's `_dropped`. Every string also passes through `assistant.redact`. Nothing leaves the Mac: the module
imports no network library and has no exporter.

Records go to `~/.assistant/telemetry/telemetry.db` (SQLite, write-ahead logging). When the database can't be
written, a record goes to `spool.jsonl` beside it and `maintain()` moves it in later. `emit()` never raises and
never waits longer than a short deadline. After one write that misses the database, this process sends its records
straight to the spool for SPOOL_ONLY_SECONDS, so a busy database costs one wait and not one for every record. `upkeep()` is the hourly call the pulse makes; it runs `maintain()`.

Strict mode: with the environment variable ASSISTANT_TELEMETRY_STRICT=1, `emit()` raises ValueError naming an
unknown kind or a dropped field instead of storing less. The tests set it, so a mistyped field name fails the
test of the code that wrote it.

Standard library only, plus `assistant.redact` and `assistant.pointers`, and it loads on the oldest Python a
service can fall back to (3.9), like redact.py.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import sqlite3
import time
import types

from assistant import pointers, redact

SCHEMA_VERSION = 1
APPLICATION_ID = 0x41535354  # "ASST": a file with another id is somebody else's database
BUSY_SECONDS = 0.2
EMIT_DEADLINE_SECONDS = 0.27  # the whole of one emit(): the database wait plus the spool lock
SPOOL_ONLY_SECONDS = 30  # after a write that misses the database, this process sends records straight to the spool
COUNT_WAIT_SECONDS = 0.03  # on top of that, to note a loss
CONFIG_RETRY_SECONDS = 0.02  # before reading a settings file that didn't parse a second time
SPOOL_LIMIT_BYTES = 5 * 1024 * 1024
KEEP_CORRUPT_COPIES = 2
UPKEEP_EVERY_SECONDS = 3600
DEFAULT_KEEP_DAYS = 90
DEFAULT_MAX_MB = 200
KEEP_DAYS_RANGE = (1, 3650)
MAX_MB_RANGE = (1, 100000)
DELETE_BATCH = 100
QUERY_LIMIT_MAX = 10000
STRICT_ENV = "ASSISTANT_TELEMETRY_STRICT"

INT, NUM, BOOL, ID, NAME, ERROR = "int", "num", "bool", "id", "name", "error"

# Lists of the values a field may take. A field of one of these types takes only a value on its list, even one
# that looks like an id. Later changes add values here, one per line, in review. The lists follow what the code
# already uses: lanes and dispositions in work_queue/triage.py, sources in work_queue/config.py, tiers in
# src/assistant/model_tiers.py, send modes in work_queue/server.py; the rest are short and sensible.
OUTCOME = ("done", "failed", "skipped", "refused", "timeout", "unknown")
SOURCE = ("slack", "gmail", "calendar", "outlook", "github")
PROVIDER = SOURCE + ("claude", "copilot")
LANE = ("do_first", "promise", "week", "optional")
DISPOSITION = ("you", "agent", "handoff", "drop")
TIER = ("frontier", "balanced", "cheap")
SEND_MODE = ("assist", "handsoff")
PULSE_MODE = ("default", "pro")
# The statuses the task file really holds (bin/todo-server.py, bin/pulse.py, work_queue/todo_result.py, skills/todo).
TASK_STATE = ("open", "in-progress", "blocked", "deferred", "done", "removed")
ORIGIN = SOURCE + ("work-queue", "goal", "manual", "agent")
PURPOSE = ("triage", "observer", "brief", "narrate", "planner", "strategist", "lesson", "draft", "other")
# The classes the effects ledger writes.
EFFECT_CLASS = ("gmail.archive", "gmail.unarchive", "gmail.mark_read", "gmail.trash", "gmail.untrash",
                "mail.unsubscribe", "slack.save.complete", "slack.save.uncomplete", "session.type")
EFFECT_OUTCOME = ("done", "failed", "unknown", "refused")
GUARD = ("interrupt_gate", "no_close_workspace", "meta_gate", "busy_session_cap", "outbound_gate", "back_off")
GUARD_OUTCOME = ("allowed", "held", "blocked", "unknown")
# Why a source couldn't be read (work_queue/tools.py, the Slack client), why a row came back from the model with no
# verdict (work_queue/triage.py), and why a dispatch didn't end well (bin/pulse.py `dispatch_todo`).
SOURCE_REASON = ("not_signed_in", "tool_missing", "timeout", "bad_answer", "busy", "other")
UNSORTED_REASON = ("call_failed", "bad_answer", "not_answered", "out_of_time", "spend_limit", "no_model")
DISPATCH_REASON = ("session_tool_down", "no_workspace", "no_surface", "session_not_ready", "todo_not_marked", "other")
# What the owner did (feedback.signal), why (reason_code), where, and on what kind of thing.
SIGNAL = ("done", "drop", "snooze", "undo", "send", "send_handsoff", "approve", "keep", "archive", "mark_read",
          "used_draft", "kept_result", "thumbs_down", "tell", "retract")
FEEDBACK_REASON = ("none", "not_important", "wrong_order", "not_mine", "already_done", "bad_draft",
                   "not_what_i_asked", "should_not_have_started", "should_not_have_pinged",
                   "wrong_tone", "wrong_facts", "not_how_id_say", "didnt_finish")
SURFACE = ("work-queue", "dashboard", "slack", "voice", "cli")
SUBJECT_KIND = ("row", "start_here", "email", "candidate", "result", "draft", "page", "task")
REASON = ("none", "policy", "user", "limit", "budget", "duplicate", "stale", "unsafe", "not_signed_in", "unknown")
ERROR_WORDS = ("timeout", "network", "refused", "not_found", "other")  # when there is no exception class to name
COMPONENTS = ("pulse", "work-queue", "dashboard", "reactor", "comms", "watcher", "self-update", "installer", "cli")
# `subject`, and a field that points at a thing, is an id: one of these prefixes and then the body that prefix has
# (`_ID_BODIES`; `tr-` and `tl-` are trace ids, see pointers.is_trace_id).
ID_PREFIXES = ("row:", "td-", "tr-", "tl-", "ef-", "fb-", "who:", "workspace:", "dec-")

# What each kind of record may hold. Adding a field is one line. A field is a number (INT, NUM), a yes/no (BOOL), a
# value from a list above, an ID, or a NAME. A NAME is an open name, kept to the few fields whose vocabulary really
# is open; each is marked below. An ERROR is an exception class name (`sqlite3.OperationalError`) or a word from
# ERROR_WORDS. A field can't be called `kind`, `trace`, `subject` or `component` (those are
# `emit()`'s own arguments) and can't start with `_` (reserved: `_dropped`). `outcome` is how something ended;
# `ok` is the yes/no. The registry is read-only: it changes in this file, in review, not at run time.
KINDS = types.MappingProxyType({name: types.MappingProxyType(spec) for name, spec in {
    "source_fetch": {
        "source": SOURCE,
        "outcome": OUTCOME,
        "ok": BOOL,
        "items": INT,
        "new_items": INT,
        "ms": INT,
        "status": INT,
        "error": ERROR,
        "reason": SOURCE_REASON,
    },
    "triage_verdict": {
        "verdict": DISPOSITION + ("unsorted",),  # `unsorted`: the row was put to the model and came back with none
        "reason": UNSORTED_REASON,  # why it is unsorted
        "batch": INT,  # which of the run's model calls this was: ties the verdict to its `model_call`
        "lane": LANE,
        "reason_code": REASON,
        "score": NUM,
        "model": NAME,  # open: a model's id, which the provider names
        "prompt_version": NAME,  # open: names a prompt file revision
        "source": SOURCE,
        "ms": INT,
        "run_trace": ID,  # the refresh's trace: ties a verdict to the model call that produced it
    },
    "model_call": {
        "model": NAME,  # open: a model's id, which the provider names
        "tier": TIER,
        "purpose": PURPOSE,
        "outcome": OUTCOME,
        "ok": BOOL,
        "tokens_in": INT,
        "tokens_out": INT,
        "cache_read": INT,
        "cache_write": INT,
        "cost_usd": NUM,
        "ms": INT,
        "error": ERROR,
        "retries": INT,
        "reason": SOURCE_REASON,  # only on a call that failed: the same words a source fetch uses
        "batch": INT,  # which of the run's model calls this was (the `triage_verdict` that came of it has the same)
    },
    "task": {
        "state": TASK_STATE,
        "prev_state": TASK_STATE,
        "origin": ORIGIN,
        "mode": SEND_MODE,
        "attempt": INT,
    },
    "dispatch": {
        "outcome": OUTCOME,
        "ok": BOOL,
        "reason": DISPATCH_REASON,  # only on a dispatch that failed (`ok` false)
        "confirmed": BOOL,  # on a dispatch that went out: whether the code saw the prompt arrive
        "run_trace": ID,  # the pulse's trace
        "workspace": ID,
        "mode": SEND_MODE,
        "attempt": INT,
        "ms": INT,
    },
    "check": {
        "name": NAME,  # open: each check names itself
        "outcome": OUTCOME,
        "ok": BOOL,
        "count": INT,
        "ms": INT,
    },
    "effect": {
        "action": EFFECT_CLASS,
        "outcome": EFFECT_OUTCOME,
        "ok": BOOL,
        "target": ID,
        "reversible": BOOL,
        "count": INT,
    },
    "feedback": {
        "signal": SIGNAL,
        "reason_code": FEEDBACK_REASON,
        "surface": SURFACE,
        "subject_kind": SUBJECT_KIND,
    },
    "guard": {
        "name": GUARD,
        "outcome": GUARD_OUTCOME,
        "ok": BOOL,
        "reason_code": REASON,
        "count": INT,
    },
    "job_run": {
        "job": NAME,  # open: each job names itself
        "outcome": OUTCOME,
        "ok": BOOL,
        "ms": INT,
        "items": INT,
        "drained": INT,
        "deleted": INT,
        "lost": INT,
        "db_kb": INT,
        "partial": BOOL,
        "error": ERROR,
    },
    "sign_in": {
        "provider": PROVIDER,
        "outcome": OUTCOME,
        "ok": BOOL,
        "ms": INT,
        "expires_in": INT,
    },
}.items()})

# A pointer is the shape both an id and an open name must have: an id-like word, never prose, a link, an address or a path: 1 to 120 characters, letters, digits and
# `_ . : / -`, starting with a letter or digit, and optionally ending in `@` and 6 to 8 digits (a dated model id).
# On top of that (see `_pointer_ok`): one `/` at most, no `:/` (a scheme), and no dot followed by 2 to 6 lowercase
# letters at the end or before a `/` (a host or file name).
_POINTER = re.compile(r"(?=.{1,120}\Z)[A-Za-z0-9][A-Za-z0-9_.:/-]*(?:@[0-9]{6,8})?")
_HOST_OR_FILE = re.compile(r"\.[a-z]{2,6}(?:/|\Z)")
_VERSION_SUFFIX = re.compile(r":[0-9]{1,2}\Z")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_DIGITS = re.compile(r"[0-9]{1,9}")
_SESSION_DIGITS = re.compile(r"[0-9]{1,18}")  # cmux 0.65 numbers sessions from 1000010000: ten digits
# The body each id prefix has. `dec-` is what src/assistant/decisions.py mints: 16 hex characters of a SHA-256.
_ID_BODIES = {"row:": _HEX16, "who:": _HEX16, "ef-": _HEX16, "fb-": _HEX16, "dec-": _HEX16, "td-": _DIGITS,
              "workspace:": _SESSION_DIGITS}
_ERROR_CLASS = re.compile(r"(?:[a-z][a-z0-9_]*\.)*[A-Z][A-Za-z0-9]*")
_ID = re.compile(r"[0-9a-f]{16}")
_COPY = re.compile(r"telemetry\.db\.corrupt-[0-9]{15}")
_MAGIC = b"SQLite format 3\x00"

_DB = "telemetry.db"
_SPOOL = "spool.jsonl"
_CLAIMED = "spool.draining."
_COUNTER = "dropped.json"
_COUNTER_LOCK = "dropped.lock"
_STAMP = "upkeep-at"
_LOCK = "spool.lock"
_RECOVER_LOCK = "recover.lock"
_LOSS_REASONS = {
    "spool_full": "%d records were dropped because the waiting list was full",
    "spool_busy": "%d records were dropped because the waiting list was busy",
    "bad_line": "%d unreadable records were discarded from the waiting list",
    "unknown_kind": "%d records were dropped because their kind is unknown",
    "set_aside": "the database was damaged and set aside, and its records are no longer used (%d lost)",
}

# One transaction, so another process never sees a half-made store. The write-ahead log is switched on after it,
# so the file's own header already holds the schema version and the application id.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, ts REAL NOT NULL,"
    " kind TEXT NOT NULL, trace TEXT NOT NULL, subject TEXT NOT NULL, component TEXT NOT NULL,"
    " fields TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS events_trace ON events (trace)",
    "CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts)",
    "CREATE INDEX IF NOT EXISTS events_ts ON events (ts)",
    "PRAGMA application_id=%d" % APPLICATION_ID,
    "PRAGMA user_version=%d" % SCHEMA_VERSION,
)
_INSERT = "INSERT OR IGNORE INTO events (id, ts, kind, trace, subject, component, fields) VALUES (?,?,?,?,?,?,?)"


class _Problem(Exception):
    """Something to tell the owner in plain words: the message is the sentence."""


class _NewerSchema(_Problem):
    pass


class _ForeignDatabase(_Problem):
    pass


class _Strict(ValueError):
    pass


def _home() -> str:
    return os.environ.get("HOME") or os.path.expanduser("~")


def folder() -> str:
    return os.path.join(_home(), ".assistant", "telemetry")


def _path(name: str) -> str:
    return os.path.join(folder(), name)


# ---------------------------------------------------------------- what a record may hold


def _pointer_ok(value) -> bool:
    return (type(value) is str and _POINTER.fullmatch(value) is not None and value.count("/") <= 1
            and _HOST_OR_FILE.search(value) is None)


def _clean_id(value, allow_empty: bool = False):
    """`value` if it is an id: one of ID_PREFIXES and then the body that prefix has (hex or digits, so no secret fits).
    Only an exact `str` is accepted, so a subclass can't hand over something else when it is turned into text."""
    if allow_empty and type(value) is str and value == "":
        return ""
    if not _pointer_ok(value):
        return None
    prefix = next((p for p in ID_PREFIXES if value.startswith(p)), None)
    if prefix is None:
        return None
    body_ok = (pointers.is_trace_id(value) if prefix in ("tr-", "tl-")
               else _ID_BODIES[prefix].fullmatch(value[len(prefix):]) is not None)
    return value if body_ok else None


def _clean_name(value):
    """`value` if it is an open name: pointer-shaped, with a letter, no run of nine digits, at most five `-` or `_`
    (a phrase has more), a `:` only as `:` and one or two digits at the end (a model's `v2:0`), nothing
    capitalised before a `/` (`feat/trace-ids`, not `Users/ann`), and not starting with an id prefix. It is a
    shape, not a proof: a short lowercase phrase would pass, which is why the open names are few."""
    if not _pointer_ok(value) or redact.redact(value) != value or value.startswith(ID_PREFIXES):
        return None
    shape = (re.search("[A-Za-z]", value) and not re.search("[0-9]{9}", value)
             and value.count("-") + value.count("_") <= 5 and ":" not in _VERSION_SUFFIX.sub("", value)
             and ("/" not in value or value.partition("/")[0] == value.partition("/")[0].lower()))
    return value if shape else None


def _clean_error(value):
    """`value` if it is an exception class name, with its module path if it has one, or one of ERROR_WORDS."""
    if type(value) is str and value in ERROR_WORDS:
        return value
    return value if _pointer_ok(value) and _ERROR_CLASS.fullmatch(value) and redact.redact(value) == value else None


def _clean_value(kind, value):
    if kind == BOOL:
        return value if type(value) is bool else None
    if kind == INT:
        return value if type(value) is int and abs(value) < 2 ** 62 else None
    if kind == NUM:
        ok = type(value) in (int, float) and math.isfinite(value)
        return value if ok else None
    if kind == ID:
        return _clean_id(value)
    if kind == NAME:
        return _clean_name(value)
    if kind == ERROR:
        return _clean_error(value)
    if type(kind) is tuple:  # a list of the values the field may take
        return value if type(value) is str and value in kind else None
    return None  # a field the kind doesn't list


def _clean_component(value):
    return value if type(value) is str and (value == "" or value in COMPONENTS) else None


def _build(kind, trace, subject, component, fields, carried: int = 0):
    """(the record to store, what was left out). The record is None for an unknown kind."""
    registry = KINDS.get(kind) if type(kind) is str else None
    if registry is None:
        return None, ["unknown kind " + (repr(kind[:40]) if type(kind) is str else type(kind).__name__)]
    left_out = []
    kept = {}
    for name, value in fields.items():
        clean = _clean_value(registry.get(name) if type(name) is str else None, value)
        if clean is None:
            left_out.append("field %s of %s" % (name if type(name) is str else "?", kind))
        else:
            kept[name] = clean
    parts = {"trace": trace if type(trace) is str and (trace == "" or pointers.is_trace_id(trace)) else None,
             "subject": _clean_id(subject, True), "component": _clean_component(component)}
    left_out += [name + " of " + kind for name, part in parts.items() if part is None]
    dropped = carried + len(left_out)
    if dropped:
        kept["_dropped"] = dropped
    record = {"kind": kind, "fields": kept, **{name: "" if part is None else part for name, part in parts.items()}}
    return record, left_out


def _stamp(record: dict) -> dict:
    record["id"] = os.urandom(8).hex()
    record["ts"] = time.time()
    return record


def _row(record: dict) -> tuple:
    return (record["id"], record["ts"], record["kind"], record["trace"], record["subject"], record["component"],
            json.dumps(record["fields"], sort_keys=True, separators=(",", ":"), allow_nan=False))


# ---------------------------------------------------------------- settings


def _in_range(value, low, high) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _read_block() -> dict:
    with open(os.path.join(_home(), ".assistant", "config.json")) as handle:
        block = json.load(handle).get("telemetry", {})
    if not isinstance(block, dict):
        raise TypeError("the telemetry setting isn't an object")
    return block


def _settings() -> dict:
    """The settings with their defaults. `problems` names each one that couldn't be read, and a setting that can't
    be read never deletes anything. `enabled` is true when the key is absent or exactly true. A `config.json` that
    exists but can't be read, or isn't an object, is a privacy switch nobody can read: recording is off
    (`unreadable` says why) until it is fixed. A missing file means on. A file that doesn't parse is read once
    more after a moment, in case it was caught while being rewritten."""
    found = {"enabled": True, "keep_days": DEFAULT_KEEP_DAYS, "max_mb": DEFAULT_MAX_MB, "problems": {},
             "unreadable": None}
    try:
        try:
            block = _read_block()
        except ValueError:
            time.sleep(CONFIG_RETRY_SECONDS)
            block = _read_block()
    except FileNotFoundError:
        return found
    except Exception as exc:  # noqa: BLE001 - a deeply nested file raises RecursionError; it is unreadable too
        why = "config.json can't be read: " + type(exc).__name__
        found.update(enabled=False, unreadable=why, problems={"enabled": why, "keep_days": why, "max_mb": why})
        return found
    found["enabled"] = block.get("enabled", True) is True
    for name, (low, high) in (("keep_days", KEEP_DAYS_RANGE), ("max_mb", MAX_MB_RANGE)):
        value = block.get(name, found[name])
        if _in_range(value, low, high):
            found[name] = value
        else:
            found["problems"][name] = "%s isn't a number from %d to %d" % (name, low, high)
    return found


def _clamped(now) -> float:
    """`now` if it is a time that isn't in the future, else the clock."""
    clock = time.time()
    return min(float(now), clock) if type(now) in (int, float) and math.isfinite(now) else clock


# ---------------------------------------------------------------- files


def _ensure_dir() -> str:
    path = folder()
    for each in (os.path.dirname(path), path):
        with contextlib.suppress(FileExistsError):  # another process made it first
            os.mkdir(each, 0o700)
    os.chmod(path, 0o700)
    return path


def _private(path: str, flags: int) -> int:
    """Open (creating if need be) with owner-only access, and tighten a file that already existed looser. Every
    file this module makes or opens for writing goes through here."""
    fd = os.open(path, flags | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _touch(name: str) -> str:
    path = _path(name)
    os.close(_private(path, os.O_RDWR))
    return path


@contextlib.contextmanager
def _locked(name: str, wait: float = BUSY_SECONDS):
    """Hold the named lock file; yields False when it couldn't be taken within `wait` seconds."""
    fd = _private(_path(name), os.O_RDWR)
    deadline = time.monotonic() + wait
    got = False
    try:
        while not got:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        yield got
    finally:
        os.close(fd)


# ---------------------------------------------------------------- the database


def _check_header(path: str) -> None:
    """Judge the file from its own first bytes, before SQLite opens it: opening a database that has a leftover
    write-ahead log beside it can change it."""
    with open(path, "rb") as handle:
        head = handle.read(100)
    if len(head) == 100 and head[:16] == _MAGIC:
        # A file with no tables yet (schema cookie 0) is a store whose set-up was interrupted: ours to finish.
        empty = int.from_bytes(head[40:44], "big") == 0 and int.from_bytes(head[68:72], "big") == 0
        if int.from_bytes(head[68:72], "big") != APPLICATION_ID and not empty:
            raise _ForeignDatabase("telemetry.db belongs to another program, so Assistant left it alone")
        if int.from_bytes(head[60:64], "big") > SCHEMA_VERSION:
            raise _NewerSchema("telemetry.db was written by a newer Assistant, so this one left it alone")


def _read_only_uri(path: str, immutable: bool = False) -> str:
    """`immutable` reads the file as it is and makes no log or index files beside it."""
    return ("file:" + path.replace("%", "%25").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
            + ("&immutable=1" if immutable else ""))


def _connect(create: bool = True):
    """A new connection, schema ready. Raises _NewerSchema or _ForeignDatabase for a file that isn't ours to
    change: its header is read, and then its version through a read-only connection (a newer version may be only
    in the log, and closing a writing connection would copy the log into the file), before the file's mode is
    touched or SQLite opens it for writing."""
    path = _path(_DB)
    with contextlib.suppress(FileNotFoundError):
        _check_header(path)
    if create:
        _touch(_DB)
    probe = sqlite3.connect(_read_only_uri(path), uri=True, timeout=BUSY_SECONDS)
    try:
        version = probe.execute("PRAGMA user_version").fetchone()[0]
    finally:
        probe.close()
    if version > SCHEMA_VERSION:
        raise _NewerSchema("telemetry.db was written by a newer Assistant, so this one left it alone")
    conn = sqlite3.connect(path, timeout=BUSY_SECONDS, isolation_level=None)
    try:
        if create and version < SCHEMA_VERSION:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("COMMIT")
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(path + suffix, 0o600)
        return conn
    except BaseException:
        conn.close()
        raise


def _is_corrupt(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.DatabaseError) and ("not a database" in text or "malformed" in text)


def _inode(path: str):
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def _copy_stamp() -> str:
    return "%015d" % int(time.time() * 1000)


def _records_in(path: str) -> int:
    """How many records a set-aside file holds, when it can be read cheaply; 0 when it can't."""
    try:
        conn = sqlite3.connect(_read_only_uri(path, True), uri=True, timeout=0)
        try:
            return int(conn.execute("SELECT count(*) FROM events").fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a damaged file is the usual reason it can't be read
        return 0


def _quarantine(seen_inode, deadline=None):
    """Move the corrupt database aside and keep the newest copies; the loss is counted. Returns True once it is
    safe to try again (including when another process already replaced the file this one saw, which is left
    alone), and None when the lock for it couldn't be taken in time: then nothing is moved."""
    path, db = folder(), _path(_DB)
    wait = BUSY_SECONDS if deadline is None else max(0.0, deadline - time.monotonic())
    with _locked(_RECOVER_LOCK, wait) as got:
        if not got:
            return None
        if _inode(db) != seen_inode:
            return True
        os.chmod(db, 0o600)
        aside = "%s.corrupt-%s" % (db, _copy_stamp())
        os.replace(db, aside)
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(db + suffix)
        copies = sorted(name for name in os.listdir(path) if _COPY.fullmatch(name))
        for name in copies[:-KEEP_CORRUPT_COPIES]:
            os.unlink(os.path.join(path, name))
    _count_loss("set_aside", _records_in(aside) or 1)
    return True


def _once(work):
    conn = _connect()
    try:
        return work(conn)
    finally:
        conn.close()


def _run(work, deadline=None):
    """`work(connection)`. If the database turns out to be corrupt, at any point, move it aside, start a fresh one
    and run it once more, unless `deadline` (a time on the monotonic clock) has passed."""
    seen = _inode(_path(_DB))
    try:
        return _once(work)
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt(exc) or not _quarantine(seen, deadline):
            raise
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("telemetry ran out of time")
    return _once(work)


# ---------------------------------------------------------------- losses, and the spool


def _read_counter() -> dict:
    try:
        with open(_path(_COUNTER)) as handle:
            data = json.load(handle)
        return {k: v for k, v in data.items() if type(v) is int and v >= 0}
    except Exception:  # noqa: BLE001 - a damaged counter starts again from zero
        return {}


def _write_counter(counts: dict) -> None:
    fd = _private(_path(_COUNTER), os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, json.dumps(counts).encode())
    finally:
        os.close(fd)


def _count_loss(reason: str, n: int = 1) -> None:
    """Note a record that was lost, so the loss shows later. Best effort: it never waits long and never raises."""
    try:
        _ensure_dir()
        with _locked(_COUNTER_LOCK, COUNT_WAIT_SECONDS) as got:
            if got:
                counts = _read_counter()
                counts[reason] = counts.get(reason, 0) + n
                _write_counter(counts)
    except Exception:  # noqa: BLE001
        pass


def _tally_losses() -> tuple:
    """(all losses so far, a sentence for each kind of loss since the last time this was asked)."""
    sentences = []
    with _locked(_COUNTER_LOCK) as got:
        counts = _read_counter()
        for reason, sentence in _LOSS_REASONS.items():
            new = counts.get(reason, 0) - counts.get("seen_" + reason, 0)
            if new > 0:
                sentences.append("telemetry: " + sentence % new)
            counts["seen_" + reason] = counts.get(reason, 0)
        if got:
            _write_counter(counts)
    return sum(counts.get(reason, 0) for reason in _LOSS_REASONS), sentences


def _spool_many(records: list, deadline: float) -> bool:
    """Append the records to the spool, in one write under one lock. False when they were dropped: over the size
    limit, the spool lock couldn't be taken before `deadline` (it is never written without the lock: another process
    may be claiming the file), or the spool can't be written. All of them are written or none, and each dropped
    record is counted."""
    try:
        lines = b"".join((json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
                         for record in records)
        _ensure_dir()
        with _locked(_LOCK, max(0.0, deadline - time.monotonic())) as got:
            if not got:
                lost = "spool_busy"
            elif os.path.getsize(_touch(_SPOOL)) + len(lines) > SPOOL_LIMIT_BYTES:
                lost = "spool_full"
            else:
                fd = _private(_path(_SPOOL), os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(fd, lines)
                finally:
                    os.close(fd)
                return True
        _count_loss(lost, len(records))
        return False
    except Exception:  # noqa: BLE001 - when the spool can't be written the records are dropped
        return False


# ---------------------------------------------------------------- writing and reading


_HEAD = ("kind", "trace", "subject", "component")
_spool_only_until: dict = {}  # database path -> when this process tries it again (see SPOOL_ONLY_SECONDS)


def _now() -> float:
    return time.monotonic()


def _insert_all(conn, rows: list) -> None:
    """One transaction for all the rows: if it fails part way, closing the connection rolls it all back."""
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(_INSERT, rows)
    conn.execute("COMMIT")


def _write(parts: list, started: float) -> bool:
    """Store the records `parts` describes, (kind, trace, subject, component, fields) each, in one go: one
    connection and one transaction, or when the database is missed one write to the spool. A record the registry
    can't build is dropped and counted without losing the others. True when every record that could be built is in
    the database, False when they were spooled, dropped, or recording is off."""
    if not _settings()["enabled"]:
        return False
    built, left_out = [], []
    for kind, trace, subject, component, fields in parts:
        record, left = _build(kind, trace, subject, component, fields)
        left_out += left
        built.append(record)
    if left_out and os.environ.get(STRICT_ENV) == "1" and "PYTEST_CURRENT_TEST" in os.environ:
        raise _Strict("telemetry would leave out: " + ", ".join(left_out))
    for _ in [r for r in built if r is None]:
        _count_loss("unknown_kind")
    records = [r for r in built if r is not None]
    if not records:
        return False
    _ensure_dir()
    records = [_stamp(record) for record in records]
    deadline = started + EMIT_DEADLINE_SECONDS
    where = _path(_DB)
    if _now() < _spool_only_until.get(where, 0.0):
        _spool_many(records, deadline)  # the database missed a moment ago: don't wait for it again yet
        return False
    try:
        rows = [_row(record) for record in records]
        _run(lambda conn: _insert_all(conn, rows), deadline)
        _spool_only_until.pop(where, None)
        return True
    except Exception:  # noqa: BLE001 - locked, full, newer schema, anything: the spool keeps them
        _spool_only_until[where] = _now() + SPOOL_ONLY_SECONDS
        _spool_many(records, deadline)
        return False


def emit(*args, **kwargs) -> bool:
    """Record one event: `emit(kind, trace, subject, component, **fields)`, where the first four may also be given
    by keyword. True when it is in the database; False when it was spooled, dropped, or recording is off. Never
    raises, whatever it is given, and never waits longer than a short deadline for the database and the spool
    (EMIT_DEADLINE_SECONDS). Any keyword that isn't one of the four is a field, and so is a fifth positional.

    `kind` must be a key of KINDS and the fields its fields. `trace` is a trace id. `subject` is an id with a
    prefix from ID_PREFIXES (a Work queue row is `assistant.pointers.row_pointer(key)`, never its key).
    `component` is one of COMPONENTS. Anything else is left out and counted in the stored record's `_dropped`.

    It is a batch of one: see `emit_many`, which writes a list the same way.

    For tests only: with ASSISTANT_TELEMETRY_STRICT=1 and a test running (PYTEST_CURRENT_TEST set) it raises
    ValueError naming whatever would be left out. A running service never raises."""
    started = time.monotonic()
    try:
        head = dict(zip(_HEAD, args))
        fields = {"arg%d" % n: value for n, value in enumerate(args[len(_HEAD):], len(_HEAD))}
        for name, value in kwargs.items():
            if name in _HEAD and name not in head:
                head[name] = value
            else:
                fields[name] = value
        return _write([(head.get("kind"), head.get("trace", ""), head.get("subject", ""), head.get("component", ""),
                        fields)], started)
    except _Strict:
        raise
    except Exception:  # noqa: BLE001 - recording never breaks work
        return False


def emit_many(batch) -> bool:
    """Record a list of events in one go: `batch` holds (kind, fields) pairs, `fields` taking `trace`, `subject`
    and `component` as well as the kind's own fields, as the keywords of `emit` do. They are stored in order, in one
    connection and one transaction, and the whole batch has the one deadline `emit` has. Each record is checked
    on its own: one that can't be built is left out (or counted, and raised in tests' strict mode) without losing the
    rest. When the database is missed the whole batch goes to the spool in one write, and for the next
    SPOOL_ONLY_SECONDS later records do too. True when every record that could be built is in the database. Never
    raises."""
    started = time.monotonic()
    try:
        parts = []
        for item in batch:
            try:
                kind, fields = item
                fields = dict(fields)
            except Exception:  # noqa: BLE001 - an item that isn't a pair is one record lost, not the batch
                _count_loss("unknown_kind")
                continue
            parts.append((kind, fields.pop("trace", ""), fields.pop("subject", ""), fields.pop("component", ""), fields))
        return _write(parts, started) if parts else False
    except _Strict:
        raise
    except Exception:  # noqa: BLE001 - recording never breaks work
        return False


def events(trace=None, kind=None, subject=None, since=None, limit=200, *, until=None, component=None,
           newest_first=False) -> list:
    """Records as dicts with `id`, `ts`, `kind`, `trace`, `subject`, `component` and `fields`: the oldest `limit`
    that match, oldest first, or with `newest_first` the newest `limit`, newest first. `since` and `until` are
    times in seconds. Reads the database only, not the spool: a record still in the spool shows up after the next
    `maintain()`. Read-only; an empty list on any failure."""
    try:
        where, args = [], []
        for column, value in (("trace", trace), ("kind", kind), ("subject", subject), ("component", component)):
            if value is not None:
                where.append(column + "=?")
                args.append(value if isinstance(value, str) else None)
        for sql, value in (("ts>=?", since), ("ts<=?", until)):
            if value is not None:
                where.append(sql)
                args.append(float(value))
        limit = max(1, min(int(limit), QUERY_LIMIT_MAX))
        if not os.path.isfile(_path(_DB)):
            return []
        conn = _connect(create=False)
        try:
            conn.execute("PRAGMA query_only=ON")
            sql = "SELECT id, ts, kind, trace, subject, component, fields FROM events"
            if where:
                sql += " WHERE " + " AND ".join(where)
            order = " ORDER BY ts DESC, seq DESC LIMIT ?" if newest_first else " ORDER BY ts, seq LIMIT ?"
            rows = conn.execute(sql + order, args + [limit]).fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "trace": r[3], "subject": r[4], "component": r[5],
                 "fields": json.loads(r[6])} for r in rows]
    except Exception:  # noqa: BLE001 - reading never breaks work
        return []


def store_state() -> str:
    """Whether `events()` can be trusted to say "nothing recorded": "ok" (the store opens and can be read), "missing"
    (there is no store yet, or it has no records table yet), "newer" (a newer Assistant wrote it and this one leaves
    it alone), "foreign" (another program's file) or "unreadable" (damaged, or busy for longer than the short wait).
    `events()` gives `[]` for every one of these, so a page that wants to tell the person which one it was asks here
    as well. Never raises, and writes nothing."""
    try:
        if not os.path.isfile(_path(_DB)):
            return "missing"
        conn = _connect(create=False)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
        finally:
            conn.close()
        return "ok"
    except _NewerSchema:
        return "newer"
    except _ForeignDatabase:
        return "foreign"
    except sqlite3.OperationalError as exc:
        return "missing" if "no such table" in str(exc) else "unreadable"
    except Exception:  # noqa: BLE001 - a read that can't tell is "unreadable", never an error
        return "unreadable"


# ---------------------------------------------------------------- upkeep


def _claim_spool() -> list:
    """Rename the spool to a file of our own and return every claimed file, ours and any a crashed drain left."""
    path = folder()
    with _locked(_LOCK) as got:
        if got and os.path.exists(_path(_SPOOL)):
            os.replace(_path(_SPOOL), os.path.join(path, "%s%d.%s" % (_CLAIMED, os.getpid(), os.urandom(4).hex())))
    return sorted(os.path.join(path, n) for n in os.listdir(path) if n.startswith(_CLAIMED))


def _spool_rows(claimed: list) -> tuple:
    """The rows in the claimed files, each line cleaned again as it is read (a time later than now is clamped to
    now), and how many lines were unusable."""
    rows, bad, clock = [], 0, time.time()
    for path in claimed:
        with open(path, "rb") as handle:
            lines = handle.read().splitlines()
        for line in lines:
            try:
                raw = json.loads(line)
                fields = dict(raw["fields"])
                carried = fields.pop("_dropped", 0)
                if not (_ID.fullmatch(raw["id"]) and type(raw["ts"]) in (int, float) and math.isfinite(raw["ts"])):
                    raise ValueError("bad id or time")
                record, _ = _build(raw["kind"], raw["trace"], raw["subject"], raw["component"], fields,
                                   carried if type(carried) is int and carried > 0 else 0)
                record.update(id=raw["id"], ts=min(raw["ts"], clock))
                rows.append(_row(record))
            except Exception:  # noqa: BLE001 - one bad line costs only itself
                bad += 1
    return rows, bad


def _put_back(claimed: list) -> None:
    """A drain that failed returns what it claimed to the spool, for the next one. If the spool lock can't be
    taken the claimed files stay where they are: every drain reads them too."""
    with _locked(_LOCK) as got:
        if not got:
            return
        fd = _private(_path(_SPOOL), os.O_WRONLY | os.O_APPEND)
        try:
            for path in claimed:
                with open(path, "rb") as source:
                    os.write(fd, source.read())
        finally:
            os.close(fd)
        for path in claimed:
            os.unlink(path)


def _drain(conn) -> tuple:
    claimed = _claim_spool()
    if not claimed:
        return 0, 0
    try:
        rows, bad = _spool_rows(claimed)
        before = conn.total_changes
        # One transaction: if it fails part way, closing the connection rolls it all back.
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(_INSERT, rows)
        conn.execute("COMMIT")
        inserted = conn.total_changes - before
    except Exception:
        _put_back(claimed)
        raise
    for path in claimed:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    return inserted, bad


def _used_bytes(conn) -> int:
    pages = conn.execute("PRAGMA page_count").fetchone()[0] - conn.execute("PRAGMA freelist_count").fetchone()[0]
    return pages * conn.execute("PRAGMA page_size").fetchone()[0]


def _trim(conn, now: float, settings: dict, result: dict) -> None:
    problems = settings["problems"]
    if "keep_days" in problems:
        result["skipped"]["age"] = problems["keep_days"]
    else:
        # Age is counted from the earlier of now and the newest record, so a clock that jumps forward doesn't
        # empty the store.
        newest = conn.execute("SELECT max(ts) FROM events").fetchone()[0]
        reference = now if newest is None else min(now, newest)
        cursor = conn.execute("DELETE FROM events WHERE ts<?", (reference - settings["keep_days"] * 86400,))
        result["deleted_old"] = cursor.rowcount
    if "max_mb" in problems:
        result["skipped"]["size"] = problems["max_mb"]
    else:
        limit = settings["max_mb"] * 1024 * 1024
        while _used_bytes(conn) > limit:
            cursor = conn.execute("DELETE FROM events WHERE seq IN"
                                  " (SELECT seq FROM events ORDER BY ts, seq LIMIT ?)", (DELETE_BATCH,))
            result["deleted_size"] += cursor.rowcount
            if cursor.rowcount < 1:
                break
    conn.execute("PRAGMA incremental_vacuum").fetchall()  # without fetching, one page is freed, not all
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _upkeep(conn, now: float, settings: dict, result: dict) -> None:
    result.update(drained=0, bad_lines=0, deleted_old=0, deleted_size=0, skipped={})
    result["drained"], result["bad_lines"] = _drain(conn)
    _trim(conn, now, settings, result)
    result["db_kb"] = _used_bytes(conn) // 1024


def maintain(now=None) -> dict:
    """The hourly upkeep. Moves the spool into the database, deletes records older than `keep_days` (counted from
    the earlier of `now` and the newest record), then the oldest ones while the database is above `max_mb`, and
    checkpoints the log. Returns the counts, and in `notes` a plain sentence for each problem, once: a setting or
    a `config.json` that can't be read, a database it can't use, a damaged database set aside, records lost since
    the last time. Writes one `job_run` record about itself. `now` is never later than the clock. Never raises."""
    result = {"ok": False, "enabled": True, "drained": 0, "bad_lines": 0, "deleted_old": 0, "deleted_size": 0,
              "skipped": {}, "notes": []}
    started = time.monotonic()
    try:
        settings = _settings()
        if not settings["enabled"]:
            result["enabled"] = False
            if settings["unreadable"]:
                result["notes"].append("telemetry: %s, so recording is off until it is fixed" % settings["unreadable"])
            return result
        now = _clamped(now)
        _ensure_dir()
        _run(lambda conn: _upkeep(conn, now, settings, result))
        _spool_only_until.pop(_path(_DB), None)  # the database took the upkeep, so it takes records again
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - upkeep never breaks the pulse
        result["error"] = type(exc).__name__.lstrip("_")
        result["notes"].append(str(exc) if isinstance(exc, _Problem) else "telemetry upkeep failed: " + result["error"])
    try:
        if result["bad_lines"]:
            _count_loss("bad_line", result["bad_lines"])
        lost, sentences = _tally_losses()
    except Exception:  # noqa: BLE001
        lost, sentences = 0, []
    result["notes"] += ["telemetry: " + why for why in result["skipped"].values()] + sentences
    result["notes"] = list(dict.fromkeys(result["notes"]))  # the same sentence once
    emit("job_run", job="telemetry-maintain", ok=result["ok"], ms=int((time.monotonic() - started) * 1000),
         items=result["drained"], drained=result["drained"], deleted=result["deleted_old"] + result["deleted_size"],
         lost=lost, db_kb=result.get("db_kb", 0), partial=bool(result["skipped"]),
         **({"error": result["error"]} if "error" in result else {}))
    return result


def upkeep(now=None):
    """`maintain()`, at most once an hour: None when it isn't due yet, or recording is off (but a `config.json`
    that can't be read is reported, hourly). The time of the last run is kept in a file and written first, so a
    failing run isn't retried at once; if it can't be written the upkeep runs anyway (it is cheap). Never
    raises."""
    try:
        settings = _settings()
        if settings["enabled"]:
            stamp = _path(_STAMP)
            with contextlib.suppress(OSError):
                _ensure_dir()
        elif settings["unreadable"]:
            stamp = os.path.join(_home(), ".assistant", "telemetry-config-note-at")
        else:
            return None
        now = _clamped(now)
        try:
            with open(stamp) as handle:
                last = float(handle.read())
        except (OSError, ValueError):
            last = 0.0
        if 0 <= now - last < UPKEEP_EVERY_SECONDS:
            return None
        with contextlib.suppress(OSError):
            fd = _private(stamp, os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(fd, str(now).encode())
            finally:
                os.close(fd)
        return maintain(now)
    except Exception:  # noqa: BLE001
        return None
