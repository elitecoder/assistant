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
import os
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
def todo_lock():
    """Single-writer flock over assistant-todo.json. EVERY read-modify-write of
    the TODO store must run inside this so the read cannot validate against a
    state another writer is about to change."""
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
