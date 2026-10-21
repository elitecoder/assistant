"""Files under ~/.assistant/slack-focus/ and the small stores built on them.

  threads.json      every collected thread, bucketed
  mine.json         every message you sent in the window
  focus.json        the triaged and judged board
  judge-cache.json  verdicts keyed by candidate content
  done.json         items you checked off: {fingerprint: when}
  state.json        the current or last refresh: running, phase, ok
  refresh.log       the last refresh's output

Every write is atomic (temp file + rename), so a dashboard render never reads
a half-written file.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def base_dir(home: Path | None = None) -> Path:
    return (home or Path(os.environ.get("HOME", str(Path.home())))) / ".assistant/slack-focus"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked(path: Path, blocking: bool = True, wait: float = 0.0):
    """An exclusive flock on `path`. Non-blocking, it retries for `wait`
    seconds, then yields False if the lock is still taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    with open(path, "a+") as f:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def signature(thread: dict | None) -> str:
    """The thread's latest message as "text|time". Triage stores it on each
    row, so a row's key never depends on a later pull's thread numbering."""
    if not thread:
        return ""
    last = (thread.get("latest") or [thread["first"]])[-1]
    return last.get("text", "") + "|" + last.get("at", "")


def fingerprint(item: dict) -> str:
    """Key a checked-off item by its link plus its thread's latest message. A
    new reply or an edit changes the key, so the item comes back on its own."""
    return hashlib.sha256((item["link"] + "||" + item.get("sig", "")).encode()).hexdigest()


def set_done(home: Path | None, fp: str, done: bool) -> None:
    root = base_dir(home)
    with locked(root / "done.lock"):
        marks = read_json(root / "done.json", {})
        marks = marks if isinstance(marks, dict) else {}
        if done:
            marks[fp] = now_iso()
        else:
            marks.pop(fp, None)
        write_json(root / "done.json", marks)


STARTUP_GRACE_SEC = 60


def read_state(home: Path | None = None, now: float | None = None) -> dict:
    """The refresh state. A run holds refresh.lock while it works, so a run
    that isn't holding it a minute after it started has stopped."""
    root = base_dir(home)
    state = read_json(root / "state.json", {})
    state = state if isinstance(state, dict) else {}
    if not state.get("running"):
        return state
    with locked(root / "refresh.lock", blocking=False) as free:
        held = not free
    try:
        age = (now if now is not None else time.time()) - datetime.fromisoformat(state["started"]).timestamp()
    except (KeyError, TypeError, ValueError):
        age = float("inf")
    if not held and age > STARTUP_GRACE_SEC:
        state.update(running=False, ok=False, phase=None,
                     error=state.get("error") or "The refresh stopped before it finished.")
    return state


def write_state(home: Path | None, **fields) -> dict:
    root = base_dir(home)
    with locked(root / "state.lock"):
        state = read_json(root / "state.json", {})
        state = state if isinstance(state, dict) else {}
        state.update(fields)
        write_json(root / "state.json", state)
    return state
