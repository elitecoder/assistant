"""The trace id of each piece of work in the Work queue: minted the first time it is seen, kept in traces.json beside
the other Work queue files, and never replaced.

    traces.json   {key: {"trace": "tr-...", "seen": "YYYY-MM-DD", "first": <epoch seconds>}}

`first` is when the id was made, so a row's answer to "Why is this here?" can start with the moment Assistant first saw it
(`first_seen`). A row whose id was made before `first` existed, or adopted from `queued.json`, has none, and an older
file without the key loads unchanged.

One piece of work can be reached by more than one key: an email by its mark key and by its link, a Slack save by its
link and by its channel and time. The keys for one piece of work are given together (a group), and they all hold one
id: whichever key has an id first hands it to the others, and a key that has one is never given another. Nothing here
may hold back a refresh, a send or a pulse: a registry that is missing, damaged, locked or unwritable is skipped, and
the caller carries on.

traces.json has its own lock, not marks.lock. marks.lock is held for a moment inside a send while the TODO store is
locked, and the refresh would queue behind every mark the person makes; the two files never need to change together.
The order is always the send lock, the task-file lock, then traces.lock, and the refresh takes them in that order too.

To ask for the id of a row, use `lookup(key)`: it reads and never writes. `trace_for(key)` is for the send, which has
to give a row an id when it has none.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from assistant import todostore
from assistant.pointers import is_trace_id, new_trace_id

from work_queue import config

KEEP_DAYS = 60
MAX_ENTRIES = 5000
LOCK_WAIT = 0.5  # a send gives up on the registry after this long; it runs inside the server's lock and the task lock
REFRESH_WAIT = 10
TASK_LOCK_WAIT = 2  # the refresh gives up matching sent rows with their tasks after this long


def now() -> float:
    return time.time()  # a seam, so a test can say what time it is


def path():
    return config.data_dir() / "traces.json"


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@contextlib.contextmanager
def lock(timeout: float):
    """Hold traces.json for one read-modify-write; TimeoutError when `timeout` seconds pass first."""
    lock_file = config.data_dir() / "traces.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("traces.lock is busy") from None
                time.sleep(0.01)
        yield


def held_id(value) -> str | None:
    """The valid trace id a registry value holds, or None. A value is an entry, or a bare id."""
    found = value.get("trace") if isinstance(value, dict) else value
    return found if is_trace_id(found) else None


def is_day(value) -> bool:
    try:
        return len(value) == 10 and date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def first_of(value) -> float | None:
    """The time (epoch seconds) a registry value says its id was made, or None when it holds none or an odd one."""
    found = value.get("first") if isinstance(value, dict) else None
    return found if type(found) in (int, float) and 0 < found < 1e12 else None


def entry_of(value, day: str):
    """A registry value as an entry, or None. A bare id is adopted; a missing, odd or future `seen` counts as today; a
    `first` is kept when it is a time, and left out otherwise."""
    found = held_id(value)
    if found is None:
        return None
    seen = value.get("seen") if isinstance(value, dict) else None
    entry = {"trace": found, "seen": seen if is_day(seen) and seen <= day else day}
    if (first := first_of(value)) is not None:
        entry["first"] = first
    return entry


def read_registry() -> tuple[dict, bool]:
    """(what traces.json holds, whether it is damaged). Missing is ({}, False). Damaged means it doesn't parse or isn't
    an object. A file that can't be read at all raises OSError: the caller must not write over what it couldn't read."""
    try:
        text = path().read_text()
    except FileNotFoundError:
        return {}, False
    try:
        found = json.loads(text)
    except ValueError:
        return {}, True
    return (found, False) if isinstance(found, dict) else ({}, True)


def open_registry(day: str) -> tuple[dict, dict]:
    """(the registry as entries, what the file held). A damaged file is moved aside first (one copy kept), with one line
    in the log, so a file that merely failed to parse is never written over."""
    raw, damaged = read_registry()
    if damaged:
        os.replace(path(), path().with_name("traces.json.damaged"))
        print("Work queue: traces.json was damaged. Moved it to traces.json.damaged and started a new one.", file=sys.stderr)
    return {k: e for k, v in raw.items() if (e := entry_of(v, day))}, raw


def queued_ids() -> dict:
    """The trace id each `queued.json` entry holds (None when it holds none), by row key: also the rows it refers to."""
    queued = config.read_json(config.data_dir() / "queued.json", {})
    queued = queued if isinstance(queued, dict) else {}
    return {key: held_id(q.get("trace") if isinstance(q, dict) else None) for key, q in queued.items()}


def adopt(registry: dict, keys, held: dict, day: str, made: str = "") -> None:
    """Give every key of one piece of work the same id: the first one any key already has (in the registry, else in
    `queued.json`), else a new one. A key that has an id keeps it, and its `first`. An id made now (here, or `made`, one
    the caller just made) starts the time the row was first seen; an id found in `queued.json`, or in a file from before
    `first` existed, has none. A key that joins the chosen id takes the earliest `first` its group holds."""
    keys = [k for k in dict.fromkeys(keys) if isinstance(k, str) and k]
    found = [t for k in keys if (t := held_id(registry.get(k)) or held.get(k))]
    chosen = found[0] if found else new_trace_id()
    firsts = [f for k in keys if held_id(registry.get(k)) == chosen and (f := first_of(registry.get(k))) is not None]
    first = min(firsts) if firsts else now() if (not found or chosen == made) else None
    for key in keys:
        own = held_id(registry.get(key))
        entry = {"trace": own or chosen, "seen": day}
        when = first_of(registry.get(key)) if own else None
        if when is None and own in (None, chosen):
            when = first
        if when is not None:
            entry["first"] = when
        registry[key] = entry


def prune(registry: dict, day: str, protected: set) -> dict:
    """Drop what was not seen for KEEP_DAYS days, then the oldest beyond MAX_ENTRIES. Keys in `protected` (seen in this
    refresh, or a sent row's) are never dropped, so a refresh with more live rows than the cap keeps them all."""
    cutoff = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
    kept = {k: v for k, v in registry.items() if k in protected or v["seen"] >= cutoff}
    if len(kept) > MAX_ENTRIES:
        loose = sorted((k for k in kept if k not in protected), key=lambda k: (kept[k]["seen"], k))
        for key in loose[:len(kept) - MAX_ENTRIES]:
            del kept[key]
    if len(kept) > MAX_ENTRIES:
        print(f"Work queue: {len(kept)} rows are live, more than the {MAX_ENTRIES} the id file is meant to hold. "
              "Kept them all.", file=sys.stderr)
    return kept


def stamp(groups, day: str | None = None) -> None:
    """Give each group of keys one id if none has it, note the day they were seen, and forget what is old. Writes only
    when something changed, so the file changes at most once a day per row."""
    day = day or today()
    groups = [g for g in groups if g]
    with lock(REFRESH_WAIT):
        registry, raw = open_registry(day)
        held = queued_ids()
        for group in groups:
            adopt(registry, group, held, day)
        live = {k for g in groups for k in g if isinstance(k, str)}
        registry = prune(registry, day, live | set(held))
        if registry != raw:
            config.write_json(path(), registry)


def lookup_many(keys) -> dict:
    """{key: its id, or "" when it has none} for each key, reading traces.json once. Never writes or locks. Use this
    to ask for a page of rows at a time; `lookup` is the one-row form.

    Pass the key the page uses for the row: the link for an Open list row (Slack, GitHub), the mark key
    (`https://work-queue.local/email/` and the email's key) for an email, the candidate key for a row waiting for
    approval. The keys of one email all hold the same id, but only these are kept for it. A row and the task it became
    share one id, so a task's row is the key of the `queued.json` entry whose `id` is the task's id."""
    try:
        registry = read_registry()[0]
    except OSError:
        return {key: "" for key in keys}
    return {key: held_id(registry.get(key)) or "" for key in keys}


def first_seen(key: str) -> float | None:
    """When the row with this key was first seen (epoch seconds, see `first_of`), or None for a row without an id, a
    row whose id was made before this was kept, or a registry that can't be read. Reads only."""
    try:
        return first_of(read_registry()[0].get(key))
    except OSError:
        return None


def lookup(key: str) -> str:
    """The id of the row with this key (see `lookup_many` for which key), or "" when it has none. Reads only, and never
    writes or locks: for anything that wants a row's id (records, feedback). A send, which must give a row an id, uses
    `trace_for`."""
    return lookup_many([key])[key]


def trace_for(key: str, known: str | None = None, day: str | None = None, also=()) -> str:
    """The id of the row with this key, looked up here and never taken from a request. `also` are other keys of the
    same piece of work (an email's link). A row without an id gets `known` (the id its `queued.json` entry holds) or a
    new one, saved. A row sent again after its task was removed gets its old id back: it is the same piece of work.

    When the registry can't be used (locked for more than half a second, unreadable, damaged, unwritable), the id the
    registry already holds for the key is used if it can be read, before a new one is made; the task and `queued.json`
    keep it, and the next refresh adopts it."""
    chosen = known if is_trace_id(known) else None
    keys = [key, *also]
    try:
        with lock(LOCK_WAIT):
            day = day or today()
            registry, damaged = read_registry()
            registry = {k: e for k, v in registry.items() if (e := entry_of(v, day))}
            chosen = next((t for k in keys if (t := held_id(registry.get(k)))), None) or chosen
            made = "" if chosen else new_trace_id()
            chosen = chosen or made
            if not damaged:  # a damaged file is left for the refresh to move aside, never written over here
                adopt(registry, keys, {key: chosen}, day, made)
                config.write_json(path(), registry)
    except Exception:  # noqa: BLE001 — a registry that can't be used never stops a send; the traceback goes to the log
        traceback.print_exc()
        chosen = lookup(key) or chosen
    return chosen or new_trace_id()


def reconcile(day: str | None = None) -> None:
    """`reconcile_locked` for every sent row, taking the task-file lock itself. It waits at most TASK_LOCK_WAIT seconds
    for that lock and, when it can't have it, does nothing this time: the page never stalls behind another writer."""
    try:
        with todostore.todo_lock(TASK_LOCK_WAIT):
            reconcile_locked(None, day)
    except TimeoutError:
        print("Work queue: the TODO store was busy, so sent rows weren't matched with their TODOs this time.",
              file=sys.stderr)


def reconcile_locked(only=None, day: str | None = None) -> None:
    """Make each sent row, its `queued.json` entry and its task agree on one id. Call it inside `todostore.todo_lock()`.

    The row's id wins; with none, the entry's; then the task's; else a new one for all three. What already has an id
    keeps it, so when the row's and the task's differ both stay and the entry records the row's, and an id the entry
    already holds is never overwritten. This is what ties a row sent before ids existed to its task, whichever of the
    pulse and the refresh got there first. `only` is a set of row keys; None means every sent row.

    A new id is saved to the registry and `queued.json` only after the task has it: if another writer changed the task
    file meanwhile the write is discarded, and all three are left for the next refresh, so the pulse can't later give the
    task a different id. It waits at most LOCK_WAIT seconds for the registry lock, since the task-file lock is held.
    Never raises: a failure here never holds back a refresh or a send."""
    try:
        _reconcile(only, day or today())
    except Exception:  # noqa: BLE001 — the traceback goes to the log
        traceback.print_exc()


def _reconcile(only, day: str) -> None:
    queued_file = config.data_dir() / "queued.json"
    queued = config.read_json(queued_file, {})
    entries = {k: q for k, q in (queued.items() if isinstance(queued, dict) else ())
               if isinstance(q, dict) and isinstance(q.get("id"), str) and (only is None or k in only)}
    if not entries:
        return
    text = todostore.todo_path().read_text()
    store = json.loads(text)
    tasks = {i["id"]: i for bucket in ("items", "completed") for i in (store.get(bucket) or [])
             if isinstance(i, dict) and isinstance(i.get("id"), str)} if isinstance(store, dict) else {}
    with lock(LOCK_WAIT):
        registry, raw = open_registry(day)
        plan = []  # (row key, entry, id to use, whether it is new, whether the task needs it)
        for key, entry in entries.items():
            task = tasks.get(entry["id"])
            if task is None:
                continue
            row, kept, own = held_id(registry.get(key)), held_id(entry), held_id(task)
            chosen = row or kept or own or new_trace_id()
            plan.append((key, entry, task, chosen, not (row or kept or own), not own))
        if any(needs for *_, needs in plan):
            for _key, _entry, task, chosen, _new, needs in plan:
                if needs:
                    task["trace"] = chosen
            done = todostore.replace_if_unchanged(
                text, json.dumps(store, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else ""))
        else:
            done = True
        queued_changed = False
        for key, entry, _task, chosen, new, _needs in plan:
            if new and not done:
                continue  # left for the next refresh, with the registry and the entry as they were
            if not held_id(registry.get(key)):
                registry[key] = {"trace": chosen, "seen": day}
            if not held_id(entry):
                entry["trace"] = chosen
                queued_changed = True
        if registry != raw:
            config.write_json(path(), registry)
    if queued_changed:
        config.write_json(queued_file, queued)
