"""todostore — the ONE lock every writer of ~/.claude/assistant-todo.json holds.

assistant-todo.json is read-modify-written by FOUR independent code paths:

  * todo-server's routes (save_json / remove_item / set_flag / append_detail /
    dispatch_now) — the human dashboard edits;
  * pulse.py's _mark_todo_dispatched — the dispatch stamp that moves a TODO out
    of bucket_b;
  * triage.create_todo — the policy engine's todo.create standing action;
  * goals._stage_todo — the planner staging a goal step.

Before this, each did its OWN unsynchronized read→modify→write. Whenever two
overlapped (the planner staging a TODO while the server flipped a flag, or the
dispatch stamp racing a triage create) one update silently clobbered the other
— the exact lost-update class M3 flagged for decisions.jsonl, unfixed for the
TODO store (goals._stage_todo took the *goals* lock, a different namespace the
other three writers never touched). This module is the single lockfile they all
serialize on: every writer wraps its read+write in ``with todo_lock():`` so the
read and the write are ONE critical section. The kernel releases the flock on
any death, same idiom as decisions._writer_lock.

Paths are computed per-call (not module constants) so tests that point $HOME at
a tmp dir see fresh paths even when this module stays cached in sys.modules.
Pure stdlib, no LLM, never closes workspaces.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def todo_path() -> Path:
    return _home() / ".claude" / "assistant-todo.json"


def lock_path() -> Path:
    """A DEDICATED lockfile (not the json itself, which is replaced via
    tmp+os.replace — locking a file you rename out from under the lock is
    meaningless)."""
    return _home() / ".claude" / "assistant-todo.lock"


@contextlib.contextmanager
def todo_lock(timeout=None):
    """Single-writer flock over assistant-todo.json. EVERY read-modify-write of
    the TODO store must run inside this so the read cannot validate against a
    state another writer is about to change. With `timeout` (seconds) it gives up
    with TimeoutError instead of waiting for ever: for a caller whose page must
    not stall behind another writer (the Work queue's refresh)."""
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("the TODO store is locked") from None
                    time.sleep(0.01)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def trace_of(item) -> str:
    """The trace id to use for a task: its own when it has a valid one, else a stand-in made from its id and creation
    date, the same every time. The pulse stores this same stand-in on the task, so the id a caller gets before the
    pulse has stamped a task is the id it has afterwards. (A task with no id of its own has no stable stand-in.)
    Use this to read a task's id; use `traces.lookup` (Work queue) for a row's: a row and the task it became share one
    id. Nothing else should store the stand-in."""
    from assistant import pointers  # noqa: PLC0415

    item = item if isinstance(item, dict) else {}
    own = item.get("trace")
    return own if pointers.is_trace_id(own) else pointers.legacy_trace_id("task", f"{item.get('id')}\0{item.get('createdAt')}")


STALE_TMP_SECONDS = 3600


def remove_stale_temporary_files() -> None:
    """Remove this module's own temporary files (`<name>.<process id>.tmp`) a killed process left behind, once they are
    over an hour old. Another writer's names (the reactor's, or the old shared `.tmp`) are left alone."""
    target = todo_path()
    try:
        for stale in target.parent.glob(f"{target.name}.*.tmp"):
            if re.fullmatch(r"\d+", stale.name[len(target.name) + 1:-len(".tmp")]) \
                    and time.time() - stale.stat().st_mtime > STALE_TMP_SECONDS:
                stale.unlink(missing_ok=True)
    except OSError:
        pass


def replace_if_unchanged(before: str, text: str) -> bool:
    """Replace the task file with `text`, only if it still reads exactly `before`. Call it inside `todo_lock()`.

    Not every writer takes the lock (the Slack reactor doesn't), so the file is read again just before the replace; if
    another writer changed it, nothing is written and False comes back, and the caller tries again on its next pass.
    The temporary file has this process's id in its name, so it is never one another writer is also using, and it is
    removed whatever happens. A process killed between the write and the replace can leave one; the next call removes
    any of these that is over an hour old."""
    remove_stale_temporary_files()
    target = todo_path()
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        try:
            now = target.read_text()
        except OSError:
            return False
        if now != before:
            return False
        os.replace(tmp, target)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def stamp_missing_traces() -> int:
    """Give every task in `items` that has no valid trace id its stand-in id (`trace_of`); return how many. Tasks also arrive from
    agents writing the file by hand and from before ids existed. Under the same lock as every other writer, and the
    file is written only when a task needed one, with nothing else changed, not even `_lastUpdated`. If another writer
    changed the file meanwhile, nothing is written and the next pulse tries again (see replace_if_unchanged)."""
    from assistant import pointers  # noqa: PLC0415

    with todo_lock():
        try:
            text = todo_path().read_text()
            data = json.loads(text)
        except (OSError, ValueError):
            return 0
        items = data.get("items") if isinstance(data, dict) else None
        missing = [i for i in items if isinstance(i, dict) and not pointers.is_trace_id(i.get("trace"))] \
            if isinstance(items, list) else []
        for item in missing:
            # The same stand-in `trace_of` gives, so the id is stable from the first time anyone asks. A task with no
            # id of its own has no stable stand-in and gets a new one.
            item["trace"] = trace_of(item) if isinstance(item.get("id"), str) and item["id"] else pointers.new_trace_id()
        if not missing:
            return 0
        done = replace_if_unchanged(
            text, json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else ""))
        return len(missing) if done else 0
