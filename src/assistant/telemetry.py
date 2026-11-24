"""Telemetry: a local record of what Assistant did, as ids, counts, timings, states and money.

A record never holds content: no message text, title, prompt, model reason or summary, email address, file path
or link. That is enforced here, by the field registry `KINDS`, and not left to callers: a field a kind doesn't
list, or a value that isn't a number, a yes/no or a pointer (see `_pointer_ok`), is left out and counted in the
record's `_dropped`. Every string also passes through `assistant.redact`. Nothing leaves the Mac: the module
imports no network library and has no exporter.

Records go to `~/.assistant/telemetry/telemetry.db` (SQLite, write-ahead logging). When the database can't be
written, a record goes to `spool.jsonl` beside it and `maintain()` moves it in later. `emit()` never raises and
never waits longer than a short deadline. `upkeep()` is the hourly call the pulse makes; it runs `maintain()`.

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
COUNT_WAIT_SECONDS = 0.03  # on top of that, to note a loss
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

INT, NUM, BOOL, PTR = "int", "num", "bool", "ptr"

# What each kind of record may hold. Adding a field is one line. A name can't be `kind`, `trace`, `subject` or
# `component` (those are `emit()`'s own arguments) and can't start with `_` (reserved: `_dropped`). `outcome` is
# how something ended, as a pointer (`ok`, `timeout`, `HTTPError`); `ok` is the yes/no. The registry is
# read-only: it changes in this file, in review, not at run time.
KINDS = types.MappingProxyType({name: types.MappingProxyType(spec) for name, spec in {
    "source_fetch": {
        "source": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "items": INT,
        "new_items": INT,
        "ms": INT,
        "status": INT,
        "error": PTR,
    },
    "triage_verdict": {
        "verdict": PTR,
        "lane": PTR,
        "reason_code": PTR,
        "score": NUM,
        "model": PTR,
        "prompt_version": PTR,
        "source": PTR,
        "ms": INT,
    },
    "model_call": {
        "model": PTR,
        "tier": PTR,
        "purpose": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "tokens_in": INT,
        "tokens_out": INT,
        "cache_read": INT,
        "cache_write": INT,
        "cost_usd": NUM,
        "ms": INT,
        "error": PTR,
        "retries": INT,
    },
    "task": {
        "state": PTR,
        "prev_state": PTR,
        "origin": PTR,
        "mode": PTR,
        "session": PTR,
        "attempt": INT,
    },
    "dispatch": {
        "outcome": PTR,
        "ok": BOOL,
        "session": PTR,
        "workspace": PTR,
        "mode": PTR,
        "attempt": INT,
        "ms": INT,
    },
    "check": {
        "name": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "count": INT,
        "ms": INT,
    },
    "effect": {
        "action": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "target": PTR,
        "reversible": BOOL,
        "count": INT,
    },
    "feedback": {
        "verdict": PTR,
        "reason_code": PTR,
        "surface": PTR,
    },
    "guard": {
        "name": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "reason_code": PTR,
        "count": INT,
    },
    "job_run": {
        "job": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "ms": INT,
        "items": INT,
        "drained": INT,
        "deleted": INT,
        "lost": INT,
        "db_kb": INT,
        "partial": BOOL,
        "error": PTR,
    },
    "sign_in": {
        "provider": PTR,
        "outcome": PTR,
        "ok": BOOL,
        "ms": INT,
        "expires_in": INT,
    },
}.items()})

# A pointer is an id-like word, never prose, a link, an address or a path: 1 to 120 characters, letters, digits and
# `_ . : / -`, starting with a letter or digit, and optionally ending in `@` and 6 to 8 digits (a dated model id).
# On top of that (see `_pointer_ok`): one `/` at most, no `:/` (a scheme), and no dot followed by 2 to 6 lowercase
# letters at the end or before a `/` (a host or file name).
_POINTER = re.compile(r"(?=.{1,120}\Z)[A-Za-z0-9][A-Za-z0-9_.:/-]*(?:@[0-9]{6,8})?")
_HOST_OR_FILE = re.compile(r"\.[a-z]{2,6}(?:/|\Z)")
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
_LOSS_REASONS = ("spool_full", "spool_busy", "bad_line", "unknown_kind")

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
            and ":/" not in value and _HOST_OR_FILE.search(value) is None)


def _clean_pointer(value, allow_empty: bool = False):
    """`value` if it is a pointer that the redactor leaves alone, else None. A secret-shaped word is not kept in a
    redacted form: it is not a pointer, so it is dropped and counted. Only an exact `str` is accepted, so a
    subclass can't hand over something else when it is turned into text."""
    if allow_empty and type(value) is str and value == "":
        return ""
    if not _pointer_ok(value) or redact.redact(value) != value:
        return None
    return value


def _clean_value(kind, value):
    if kind == BOOL:
        return value if type(value) is bool else None
    if kind == INT:
        return value if type(value) is int and abs(value) < 2 ** 62 else None
    if kind == NUM:
        ok = type(value) in (int, float) and math.isfinite(value)
        return value if ok else None
    if kind == PTR:
        return _clean_pointer(value)
    return None  # a field the kind doesn't list


def _build(kind, trace, subject, component, fields, carried: int = 0):
    """(the record to store, what was left out). The record is None for an unknown kind."""
    registry = KINDS.get(kind) if type(kind) is str else None
    if registry is None:
        return None, ["unknown kind"]
    left_out = []
    kept = {}
    for name, value in fields.items():
        clean = _clean_value(registry.get(name) if type(name) is str else None, value)
        if clean is None:
            left_out.append("field %s of %s" % (name if type(name) is str else "?", kind))
        else:
            kept[name] = clean
    parts = {"trace": trace if type(trace) is str and (trace == "" or pointers.is_trace_id(trace)) else None,
             "subject": _clean_pointer(subject, True), "component": _clean_pointer(component, True)}
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


def _settings() -> dict:
    """The settings with their defaults. `problems` names each one that couldn't be read; a setting that can't be
    read never deletes anything. An unreadable file keeps recording (with the defaults); a present `enabled`
    that isn't the JSON value true turns recording off."""
    found = {"enabled": True, "keep_days": DEFAULT_KEEP_DAYS, "max_mb": DEFAULT_MAX_MB, "problems": {}}
    try:
        with open(os.path.join(_home(), ".assistant", "config.json")) as handle:
            block = json.load(handle).get("telemetry", {})
        if not isinstance(block, dict):
            raise ValueError("the telemetry setting isn't an object")
    except FileNotFoundError:
        return found
    except Exception as exc:  # noqa: BLE001 - a deeply nested file raises RecursionError, and still keeps recording
        why = "config.json can't be read: " + type(exc).__name__
        found["problems"] = {"enabled": why, "keep_days": why, "max_mb": why}
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
        if int.from_bytes(head[68:72], "big") != APPLICATION_ID:
            raise _ForeignDatabase("telemetry.db belongs to another program, so Assistant left it alone")
        if int.from_bytes(head[60:64], "big") > SCHEMA_VERSION:
            raise _NewerSchema("telemetry.db was written by a newer Assistant, so this one left it alone")


def _connect(create: bool = True):
    """A new connection, schema ready. Raises _NewerSchema or _ForeignDatabase for a file that isn't ours to
    change."""
    path = _touch(_DB) if create else _path(_DB)
    _check_header(path)
    conn = sqlite3.connect(path, timeout=BUSY_SECONDS, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:  # a version still in the log, not yet in the file's header
            raise _NewerSchema("telemetry.db was written by a newer Assistant, so this one left it alone")
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


def _quarantine(seen_inode) -> None:
    """Move the corrupt database aside and keep the newest copies. If another process already replaced the file
    this one saw, leave the new one alone."""
    path, db = folder(), _path(_DB)
    with _locked(_RECOVER_LOCK):
        if _inode(db) != seen_inode:
            return
        os.chmod(db, 0o600)
        os.replace(db, "%s.corrupt-%s" % (db, _copy_stamp()))
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(db + suffix)
        copies = sorted(name for name in os.listdir(path) if _COPY.fullmatch(name))
        for name in copies[:-KEEP_CORRUPT_COPIES]:
            os.unlink(os.path.join(path, name))


def _once(work):
    conn = _connect()
    try:
        return work(conn)
    finally:
        conn.close()


def _run(work):
    """`work(connection)`. If the database turns out to be corrupt, at any point, move it aside, start a fresh one
    and run it once more."""
    seen = _inode(_path(_DB))
    try:
        return _once(work)
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt(exc):
            raise
    _quarantine(seen)
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
    """(all losses so far, those since the last time this was asked)."""
    with _locked(_COUNTER_LOCK) as got:
        counts = _read_counter()
        total = sum(counts.get(reason, 0) for reason in _LOSS_REASONS)
        new = max(0, total - counts.get("reported", 0))
        if got:
            counts["reported"] = total
            _write_counter(counts)
    return total, new


def _spool(record: dict, deadline: float) -> bool:
    """Append the record to the spool. False when it was dropped: over the size limit, the spool lock couldn't be
    taken before `deadline` (it is never written without the lock: another process may be claiming the file), or
    the spool can't be written."""
    try:
        line = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        _ensure_dir()
        with _locked(_LOCK, max(0.0, deadline - time.monotonic())) as got:
            if not got:
                lost = "spool_busy"
            elif os.path.getsize(_touch(_SPOOL)) + len(line) > SPOOL_LIMIT_BYTES:
                lost = "spool_full"
            else:
                fd = _private(_path(_SPOOL), os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
                return True
        _count_loss(lost)
        return False
    except Exception:  # noqa: BLE001 - when the spool can't be written the record is dropped
        return False


# ---------------------------------------------------------------- writing and reading


def emit(kind=None, /, *, trace="", subject="", component="", **fields) -> bool:
    """Record one event. True when it is in the database; False when it was spooled, dropped, or recording is off.
    Never raises, and never waits longer than a short deadline for the database and the spool (see
    EMIT_DEADLINE_SECONDS), except in strict mode, which raises ValueError for anything that would be left out.

    `kind` must be a key of KINDS and `fields` its fields; `trace` is a trace id; `subject` and `component` are
    pointers (a Work queue row is `assistant.pointers.row_pointer(key)`, never its key). Anything else is left
    out and counted in the stored record's `_dropped`."""
    started = time.monotonic()
    try:
        if not _settings()["enabled"]:
            return False
        record, left_out = _build(kind, trace, subject, component, fields)
        if left_out and os.environ.get(STRICT_ENV) == "1":
            raise _Strict("telemetry would leave out: " + ", ".join(left_out))
        if record is None:
            _count_loss("unknown_kind")
            return False
        _ensure_dir()
        record = _stamp(record)
        try:
            _run(lambda conn: conn.execute(_INSERT, _row(record)))
            return True
        except Exception:  # noqa: BLE001 - locked, full, newer schema, anything: the spool keeps it
            _spool(record, started + EMIT_DEADLINE_SECONDS)
            return False
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
        cursor = conn.execute("DELETE FROM events WHERE ts<?", (now - settings["keep_days"] * 86400,))
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
    """The hourly upkeep. Moves the spool into the database, deletes records older than `keep_days`, then the
    oldest ones while the database is above `max_mb`, and checkpoints the log. Returns the counts, in `notes` a
    plain sentence for each problem (a setting that can't be read, which skips the deletion it controls; a
    database it can't use; records lost since the last time), and writes one `job_run` record about itself.
    `now` is never later than the clock. Never raises."""
    result = {"ok": False, "enabled": True, "drained": 0, "bad_lines": 0, "deleted_old": 0, "deleted_size": 0,
              "skipped": {}, "notes": []}
    started = time.monotonic()
    try:
        settings = _settings()
        if not settings["enabled"]:
            result["enabled"] = False
            return result
        now = _clamped(now)
        _ensure_dir()
        _run(lambda conn: _upkeep(conn, now, settings, result))
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - upkeep never breaks the pulse
        result["error"] = type(exc).__name__.lstrip("_")
        result["notes"].append(str(exc) if isinstance(exc, _Problem) else "telemetry upkeep failed: " + result["error"])
    try:
        if result["bad_lines"]:
            _count_loss("bad_line", result["bad_lines"])
        lost, new = _tally_losses()
    except Exception:  # noqa: BLE001
        lost, new = 0, 0
    result["notes"] += ["telemetry: " + why for why in result["skipped"].values()]
    if new:
        result["notes"].append("telemetry: %d records were lost since the last upkeep" % new)
    emit("job_run", job="telemetry-maintain", ok=result["ok"], ms=int((time.monotonic() - started) * 1000),
         items=result["drained"], drained=result["drained"], deleted=result["deleted_old"] + result["deleted_size"],
         lost=lost, db_kb=result.get("db_kb", 0), partial=bool(result["skipped"]),
         **({"error": result["error"]} if "error" in result else {}))
    return result


def upkeep(now=None):
    """`maintain()`, at most once an hour: None when it isn't due yet or recording is off. The time of the last run
    is kept in the telemetry folder and written first, so a failing run isn't retried at once; if it can't be
    written the upkeep runs anyway (it is cheap). Never raises."""
    try:
        if not _settings()["enabled"]:
            return None
        now = _clamped(now)
        try:
            with open(_path(_STAMP)) as handle:
                last = float(handle.read())
        except (OSError, ValueError):
            last = 0.0
        if 0 <= now - last < UPKEEP_EVERY_SECONDS:
            return None
        try:
            _ensure_dir()
            fd = _private(_path(_STAMP), os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(fd, str(now).encode())
            finally:
                os.close(fd)
        except OSError:
            pass
        return maintain(now)
    except Exception:  # noqa: BLE001
        return None
