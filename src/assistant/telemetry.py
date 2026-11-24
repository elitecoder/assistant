"""Telemetry: a local record of what Assistant did, as ids, counts, timings, states and money.

A record never holds content: no message text, title, prompt, model reason or summary, email address or link.
That is enforced here, by the field registry `KINDS`, and not left to callers: a field a kind doesn't list, or a
value that isn't a number, a yes/no or a pointer (see `_pointer_ok`), is left out and counted in the record's
`_dropped`. Every string also passes through `assistant.redact` before it is written. Nothing leaves the Mac: the
module imports no network library and has no exporter.

Records go to `~/.assistant/telemetry/telemetry.db` (SQLite, write-ahead logging). When the database can't be
written, a record goes to `spool.jsonl` beside it and `maintain()` moves it in later. `emit()` never raises and
never waits longer than the busy timeout. `maintain()` is the hourly upkeep the pulse calls.

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

from assistant import pointers, redact

SCHEMA_VERSION = 1
BUSY_SECONDS = 0.2
SPOOL_LIMIT_BYTES = 5 * 1024 * 1024
KEEP_CORRUPT_COPIES = 2
DEFAULT_KEEP_DAYS = 90
DEFAULT_MAX_MB = 200
DELETE_BATCH = 1000
QUERY_LIMIT_MAX = 10000

INT, NUM, BOOL, PTR = "int", "num", "bool", "ptr"

# What each kind of record may hold. Adding a field is one line. Names can't be `kind`, `trace`, `subject` or
# `component`, which are `emit()`'s own arguments. A pointer is an id-like word (see `_pointer_ok`), so states,
# outcomes and error classes are pointers too: `ok`, `timeout`, `HTTPError`.
KINDS = {
    "source_fetch": {
        "source": PTR,
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
        "tokens_in": INT,
        "tokens_out": INT,
        "cache_read": INT,
        "cache_write": INT,
        "cost_usd": NUM,
        "ms": INT,
        "ok": BOOL,
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
        "session": PTR,
        "workspace": PTR,
        "mode": PTR,
        "attempt": INT,
        "ms": INT,
        "ok": BOOL,
    },
    "check": {
        "name": PTR,
        "result": PTR,
        "ok": BOOL,
        "count": INT,
        "ms": INT,
    },
    "effect": {
        "action": PTR,
        "outcome": PTR,
        "target": PTR,
        "ok": BOOL,
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
        "decision": PTR,
        "reason_code": PTR,
        "count": INT,
    },
    "job_run": {
        "job": PTR,
        "ok": BOOL,
        "ms": INT,
        "items": INT,
        "drained": INT,
        "deleted": INT,
        "spool_dropped": INT,
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
}

# Letters, digits and `_ . : @ # / + -`, 1 to 120 of them. No space, quote, `<`, `=`, `?` or `&`, so neither prose
# nor a query string fits; `//` is refused too, so a link doesn't.
_POINTER = re.compile(r"[A-Za-z0-9_.:@#/+-]{1,120}")
_ID = re.compile(r"[0-9a-f]{16}")

_DB = "telemetry.db"
_SPOOL = "spool.jsonl"
_CLAIMED = "spool.draining."
_COUNTER = "dropped.json"
_LOCK = "spool.lock"
_RECOVER_LOCK = "recover.lock"

_SCHEMA = (
    "PRAGMA auto_vacuum=INCREMENTAL",
    "PRAGMA journal_mode=WAL",
    "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,"
    " trace TEXT NOT NULL, subject TEXT NOT NULL, component TEXT NOT NULL, fields TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS events_trace ON events (trace)",
    "CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts)",
    "CREATE INDEX IF NOT EXISTS events_ts ON events (ts)",
    "PRAGMA user_version=%d" % SCHEMA_VERSION,
)
_INSERT = "INSERT OR IGNORE INTO events (id, ts, kind, trace, subject, component, fields) VALUES (?,?,?,?,?,?,?)"


class _NewerSchema(Exception):
    """The database was written by a newer version of this code. Leave it alone."""


def _home() -> str:
    return os.environ.get("HOME") or os.path.expanduser("~")


def _dir() -> str:
    return os.path.join(_home(), ".assistant", "telemetry")


def _path(name: str) -> str:
    return os.path.join(_dir(), name)


def _pointer_ok(value) -> bool:
    return isinstance(value, str) and _POINTER.fullmatch(value) is not None and "//" not in value


def _clean_pointer(value, allow_empty: bool = False):
    """`value` if it is a pointer that the redactor leaves alone, else None. A secret-shaped word is not kept
    in a redacted form: it is not a pointer, so it is dropped and counted."""
    if allow_empty and value == "":
        return ""
    if not _pointer_ok(value) or redact.redact(value) != value:
        return None
    return str(value)


def _clean_value(kind: str, value):
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
    """The record to store, or None for an unknown kind. What can't be kept is left out and counted."""
    registry = KINDS.get(kind) if isinstance(kind, str) else None
    if registry is None:
        return None
    dropped = carried
    kept = {}
    for name, value in fields.items():
        try:
            clean = _clean_value(registry.get(name), value)
        except Exception:  # noqa: BLE001 - a value that misbehaves is a value that isn't kept
            clean = None
        if clean is None:
            dropped += 1
        else:
            kept[name] = clean
    if trace != "" and not pointers.is_trace_id(trace):
        trace = None
    parts = [trace, _clean_pointer(subject, True), _clean_pointer(component, True)]
    dropped += sum(1 for part in parts if part is None)
    if dropped:
        kept["_dropped"] = dropped
    trace, subject, component = ("" if part is None else part for part in parts)
    return {"kind": kind, "trace": trace, "subject": subject, "component": component, "fields": kept}


def _stamp(record: dict) -> dict:
    record["id"] = os.urandom(8).hex()
    record["ts"] = time.time()
    return record


def _row(record: dict) -> tuple:
    return (record["id"], record["ts"], record["kind"], record["trace"], record["subject"], record["component"],
            json.dumps(record["fields"], sort_keys=True, separators=(",", ":"), allow_nan=False))


def _settings() -> dict:
    """The settings with their defaults. `problems` names each one that couldn't be read; a setting that can't be
    read never deletes anything."""
    found = {"enabled": True, "keep_days": DEFAULT_KEEP_DAYS, "max_mb": DEFAULT_MAX_MB, "problems": {}}
    try:
        with open(os.path.join(_home(), ".assistant", "config.json")) as handle:
            block = json.load(handle).get("telemetry", {})
        if not isinstance(block, dict):
            raise ValueError("the telemetry setting isn't an object")
    except FileNotFoundError:
        return found
    except (OSError, ValueError, AttributeError) as exc:
        why = "config.json can't be read: " + type(exc).__name__
        found["problems"] = {"enabled": why, "keep_days": why, "max_mb": why}
        return found
    for name in ("keep_days", "max_mb"):
        value = block.get(name, found[name])
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            found[name] = value
        else:
            found["problems"][name] = name + " isn't a number above zero"
    if type(block.get("enabled", True)) is bool:
        found["enabled"] = block.get("enabled", True)
    else:
        found["problems"]["enabled"] = "enabled isn't true or false"
    return found


def _ensure_dir() -> str:
    folder = _dir()
    assistant = os.path.dirname(folder)
    for path in (assistant, folder):
        with contextlib.suppress(FileExistsError):  # another process made it first
            os.mkdir(path, 0o700)
    os.chmod(folder, 0o700)
    return folder


def _touch(name: str) -> str:
    """The file's path, created owner-only if it doesn't exist yet."""
    path = _path(name)
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    return path


@contextlib.contextmanager
def _locked(name: str):
    """Hold the named lock file; yields False when it couldn't be taken within the busy timeout."""
    fd = os.open(_touch(name), os.O_RDWR)
    deadline = time.monotonic() + BUSY_SECONDS
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


def _connect(create: bool = True):
    """A new connection, schema ready. Raises _NewerSchema for a database this code doesn't know."""
    conn = sqlite3.connect(_touch(_DB) if create else _path(_DB), timeout=BUSY_SECONDS, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise _NewerSchema()
        if create and version < SCHEMA_VERSION:
            for statement in _SCHEMA:
                conn.execute(statement)
        conn.execute("PRAGMA synchronous=NORMAL")
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


def _quarantine(seen_inode) -> None:
    """Move the corrupt database aside and keep the newest copies. If another process already replaced the file
    this one saw, leave the new one alone."""
    folder, db = _dir(), _path(_DB)
    with _locked(_RECOVER_LOCK):
        if _inode(db) != seen_inode:
            return
        os.chmod(db, 0o600)
        os.replace(db, "%s.corrupt-%015d" % (db, int(time.time() * 1000)))
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(db + suffix)
        copies = sorted(name for name in os.listdir(folder) if name.startswith(_DB + ".corrupt-"))
        for name in copies[:-KEEP_CORRUPT_COPIES]:
            os.unlink(os.path.join(folder, name))


def _connect_healing():
    """`_connect`, and when the database is corrupt, move it aside, start a fresh one and try once more."""
    seen = _inode(_path(_DB))
    try:
        return _connect()
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt(exc):
            raise
    _quarantine(seen)
    return _connect()


def _count_dropped(n: int) -> None:
    with _locked(_COUNTER) as got:
        if not got:
            return
        path = _path(_COUNTER)
        try:
            with open(path) as handle:
                total = int(json.load(handle)["spool_dropped"])
        except (OSError, ValueError, KeyError, TypeError):
            total = 0
        with open(path, "w") as handle:
            json.dump({"spool_dropped": total + n, "updated": time.time()}, handle)


def _spool(record: dict) -> bool:
    """Append the record to the spool. False when it was dropped (over the size limit, or the spool can't be
    written)."""
    try:
        line = (json.dumps(dict(record), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        _ensure_dir()
        with _locked(_LOCK):
            path = _touch(_SPOOL)
            if os.path.getsize(path) + len(line) > SPOOL_LIMIT_BYTES:
                _count_dropped(1)
                return False
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - when the spool can't be written the record is dropped
        return False


def emit(kind, trace="", subject="", component="", **fields) -> bool:
    """Record one event. True when it is in the database; False when it was spooled, dropped, or recording is off.
    Never raises, and never waits longer than the busy timeout for the database or the spool.

    `kind` must be a key of KINDS and `fields` its fields; `trace` is a trace id; `subject` and `component` are
    pointers (a Work queue row is `assistant.pointers.row_pointer(key)`, never its key). Anything else is left
    out and counted in the stored record's `_dropped`."""
    try:
        if not _settings()["enabled"]:
            return False
        record = _build(kind, trace, subject, component, fields)
        if record is None:
            return False
        _ensure_dir()
        record = _stamp(record)
        try:
            conn = _connect_healing()
            try:
                conn.execute(_INSERT, _row(record))
            finally:
                conn.close()
            return True
        except Exception:  # noqa: BLE001 - locked, full, newer schema, anything: the spool keeps it
            _spool(record)
            return False
    except Exception:  # noqa: BLE001 - recording never breaks work
        return False


def events(trace=None, kind=None, subject=None, since=None, limit=200) -> list:
    """Records oldest first, as dicts with `id`, `ts`, `kind`, `trace`, `subject`, `component` and `fields`. The
    oldest `limit` that match. Reads the database only, not the spool: a record still in the spool shows up after
    the next `maintain()`. Read-only; an empty list on any failure."""
    try:
        where, args = [], []
        for column, value in (("trace", trace), ("kind", kind), ("subject", subject)):
            if value is not None:
                where.append(column + "=?")
                args.append(value if isinstance(value, str) else None)
        if since is not None:
            where.append("ts>=?")
            args.append(float(since))
        limit = max(1, min(int(limit), QUERY_LIMIT_MAX))
        if not os.path.isfile(_path(_DB)):
            return []
        conn = _connect(create=False)
        try:
            conn.execute("PRAGMA query_only=ON")
            sql = "SELECT id, ts, kind, trace, subject, component, fields FROM events"
            if where:
                sql += " WHERE " + " AND ".join(where)
            rows = conn.execute(sql + " ORDER BY ts, rowid LIMIT ?", args + [limit]).fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "trace": r[3], "subject": r[4], "component": r[5],
                 "fields": json.loads(r[6])} for r in rows]
    except Exception:  # noqa: BLE001 - reading never breaks work
        return []


def _claim_spool() -> list:
    """Rename the spool to a file of our own and return every claimed file, ours and any a crashed drain left."""
    folder = _dir()
    with _locked(_LOCK) as got:
        if got and os.path.exists(_path(_SPOOL)):
            os.replace(_path(_SPOOL), os.path.join(folder, "%s%d.%s" % (_CLAIMED, os.getpid(), os.urandom(4).hex())))
    return sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.startswith(_CLAIMED))


def _spool_rows(claimed: list) -> tuple:
    """The rows in the claimed files, each line cleaned again as it is read, and how many lines were unusable."""
    rows, bad = [], 0
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
                record = _build(raw["kind"], raw["trace"], raw["subject"], raw["component"], fields,
                                carried if type(carried) is int and carried > 0 else 0)
                record.update(id=raw["id"], ts=raw["ts"])
                rows.append(_row(record))
            except Exception:  # noqa: BLE001 - one bad line costs only itself
                bad += 1
    return rows, bad


def _put_back(claimed: list) -> None:
    """A drain that failed returns what it claimed to the spool, for the next one."""
    with _locked(_LOCK):
        with open(_path(_SPOOL), "ab") as handle:
            for path in claimed:
                with open(path, "rb") as source:
                    handle.write(source.read())
        for path in claimed:
            os.unlink(path)


def _drain(conn) -> tuple:
    claimed = _claim_spool()
    if not claimed:
        return 0, 0
    try:
        rows, bad = _spool_rows(claimed)
        before = conn.total_changes
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(_INSERT, rows)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
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
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if "max_mb" in problems:
        result["skipped"]["size"] = problems["max_mb"]
    else:
        limit = settings["max_mb"] * 1024 * 1024
        while _used_bytes(conn) > limit:
            cursor = conn.execute("DELETE FROM events WHERE rowid IN"
                                  " (SELECT rowid FROM events ORDER BY ts, rowid LIMIT ?)", (DELETE_BATCH,))
            result["deleted_size"] += cursor.rowcount
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if cursor.rowcount < 1:
                break
        conn.execute("PRAGMA incremental_vacuum")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _dropped_total() -> int:
    try:
        with open(_path(_COUNTER)) as handle:
            return max(0, int(json.load(handle)["spool_dropped"]))
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def maintain(now=None) -> dict:
    """The hourly upkeep. Moves the spool into the database, deletes records older than `keep_days`, then the
    oldest ones while the database is above `max_mb`, and checkpoints the log. Returns the counts, and writes
    one `job_run` record about itself. A setting that can't be read skips the deletion it controls and says why
    under `skipped`. Never raises."""
    result = {"ok": False, "enabled": True, "drained": 0, "bad_lines": 0, "deleted_old": 0, "deleted_size": 0,
              "skipped": {}}
    started = time.monotonic()
    try:
        settings = _settings()
        if not settings["enabled"]:
            result["enabled"] = False
            return result
        now = float(now) if type(now) in (int, float) and math.isfinite(now) else time.time()
        _ensure_dir()
        conn = _connect_healing()
        try:
            result["drained"], result["bad_lines"] = _drain(conn)
            _trim(conn, now, settings, result)
            result["db_kb"] = _used_bytes(conn) // 1024
        finally:
            conn.close()
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - upkeep never breaks the pulse
        result["error"] = type(exc).__name__
    emit("job_run", job="telemetry-maintain", ok=result["ok"], ms=int((time.monotonic() - started) * 1000),
         items=result["drained"], drained=result["drained"], deleted=result["deleted_old"] + result["deleted_size"],
         spool_dropped=_dropped_total(), db_kb=result.get("db_kb", 0), partial=bool(result["skipped"]),
         **({"error": result["error"]} if "error" in result else {}))
    return result
