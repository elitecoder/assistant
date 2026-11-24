"""The trace id of each Work queue row: minted the first time the row is seen, kept in traces.json beside the other
Work queue files, and never replaced.

    traces.json   {row key: {"trace": "tr-...", "seen": "YYYY-MM-DD"}}

A row is rebuilt from its sources at every refresh, so its id has to live outside it. The row key is the row's link,
the same key `queued.json` and the page use. Nothing here may hold back a refresh or a send: a registry that is
missing, broken or unwritable is skipped, and the caller carries on.

traces.json has its own lock, not marks.lock. marks.lock is held for a moment inside a send while the TODO store is
locked, and the refresh would queue behind every mark the person makes; the two files never need to change together.
"""
from __future__ import annotations

import contextlib
import fcntl
import traceback
from datetime import date, datetime, timedelta, timezone

from assistant.pointers import is_trace_id, new_trace_id

from work_queue import config

KEEP_DAYS = 60
MAX_ENTRIES = 5000


def path():
    return config.data_dir() / "traces.json"


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@contextlib.contextmanager
def lock():
    """Hold traces.json for one read-modify-write."""
    lock_file = config.data_dir() / "traces.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def load() -> dict:
    """The saved registry; {} when it is missing or isn't an object."""
    found = config.read_json(path(), {})
    return found if isinstance(found, dict) else {}


def trace_of(entry) -> str | None:
    """The valid trace id an entry holds, or None."""
    found = entry.get("trace") if isinstance(entry, dict) else None
    return found if is_trace_id(found) else None


def queued_ids() -> dict:
    """The trace ids `queued.json` holds, by row key, and so the rows it still refers to."""
    queued = config.read_json(config.data_dir() / "queued.json", {})
    queued = queued if isinstance(queued, dict) else {}
    return {key: trace_of(q) for key, q in queued.items()}


def is_day(value) -> bool:
    try:
        return len(value) == 10 and date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def prune(registry: dict, day: str, held: set) -> dict:
    """Drop what was not seen for KEEP_DAYS days, unless `held`, then the oldest beyond MAX_ENTRIES (held last)."""
    cutoff = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
    kept = {k: v for k, v in registry.items() if k in held or v["seen"] >= cutoff}
    if len(kept) > MAX_ENTRIES:
        order = sorted(kept, key=lambda k: (k in held, kept[k]["seen"], k))
        kept = {k: kept[k] for k in order[len(kept) - MAX_ENTRIES:]}
    return kept


def stamp(keys, day: str | None = None) -> None:
    """Give every row key an id if it has none, note that it was seen on `day`, and forget what is old. Writes only
    when something changed, so the file changes at most once a day per row."""
    day = day or today()
    keys = [k for k in dict.fromkeys(keys) if isinstance(k, str)]
    with lock():
        before = load()
        held = queued_ids()
        registry = {}
        for key, entry in before.items():
            if trace_of(entry):
                registry[key] = {"trace": entry["trace"], "seen": entry["seen"] if is_day(entry.get("seen")) else day}
        for key in keys:
            # An id `queued.json` already holds is the row's, so a lost registry doesn't hand it a second one.
            registry[key] = {"trace": trace_of(registry.get(key)) or held.get(key) or new_trace_id(), "seen": day}
        registry = prune(registry, day, set(held))
        if registry != before:
            config.write_json(path(), registry)


def trace_for(key: str, known: str | None = None, day: str | None = None) -> str:
    """The id of the row with this key, looked up here and never taken from a request. A row without one gets
    `known` (an id its `queued.json` entry holds) or a new one, saved. When the registry can't be used, the id is
    still returned, and the task and `queued.json` keep it."""
    found = known if is_trace_id(known) else None
    try:
        with lock():
            registry = load()
            if trace := trace_of(registry.get(key)):
                return trace
            found = found or new_trace_id()
            registry[key] = {"trace": found, "seen": day or today()}
            config.write_json(path(), registry)
    except Exception:  # noqa: BLE001 — a registry that can't be used never stops a send; the traceback goes to the log
        traceback.print_exc()
    return found or new_trace_id()
