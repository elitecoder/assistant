"""failwatch: make a failure that keeps repeating visible once, with a count,
and let background work stop retrying it blindly.

Producers call `record_failure(component, text)` where something fails and
`record_ok(component)` where the same operation succeeds. A background job asks
`backed_off(component)` before it retries. The dashboard reads `repeated()`.

State lives in ~/.assistant/failures.json, one small JSON object keyed by a
fingerprint of the error (the text with numbers, ids and paths removed, so
"the same failure" collapses to one row). It is deliberately NOT written to the
actions ledger: that ledger is broadcast to Slack and counted as goal progress.

Nothing here may break its caller. Every public function swallows its own
errors and returns a neutral value.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path

MAX_FINGERPRINTS = 200
MAX_SAMPLE = 200
FINGERPRINT_TEXT = 100
BACKOFF_AFTER = 3                  # consecutive identical failures before pausing
BACKOFF_BASE_SEC = 3600            # first pause: 1 hour, doubling after that
BACKOFF_CAP_SEC = 24 * 3600
LOCK_WAIT_SEC = 0.2                # a contended store must not slow a dashboard request
MAX_SHOWN = 5

# A local guard so a token in an error message never lands in failures.json.
# The shared redactor replaces this once it lands.
_SECRETS = re.compile(
    r"xox[a-z]-\S+|Bearer\s+\S+|gh[pousr]_\S+|token=\S+"
    r"|\bsk-[\w-]{6,}"
    r"|(?:api[_-]?key|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?[^\s\"',}&]+"
    r"|(?<=://)[^/\s:@]+:[^/\s@]+(?=@)",
    re.IGNORECASE)

# A quote opens only after a non-word character, so the apostrophe in
# "couldn't" is not mistaken for the start of a quoted value.
_QUOTED = re.compile(r"""(?<!\w)'[^']*'(?!\w)|"[^"]*"|`[^`]*`""")
# A quoted value is dropped, but `"type": "overloaded_error"` is what tells a
# 529 from a 429, so it is kept as a bare word first.
_ERROR_TYPE = re.compile(r'"type"\s*:\s*"(\w*error)"', re.IGNORECASE)
# A 3-digit HTTP status after one of these words is kept (as letters, so the
# digit strip leaves it): a 401, a 429 and a 529 are different problems.
_STATUS = re.compile(r"\b(status(?:[ _]code)?|http|code|error)(\W{0,3})([45]\d\d)(?!\d)", re.IGNORECASE)
# One id per request would give every call its own fingerprint.
_REQUEST_ID = re.compile(r"\b(?:req|msg|toolu)_\w+|\brequest[-_ ]?id\W+[\w-]+", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_HOME_PATH = re.compile(r"(?:~|/Users/[^/\s]+|/home/[^/\s]+)[^\s'\")]*")
_ABS_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.\-@+]+/)+[\w.\-@+]*")
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_DIGIT_LETTERS = str.maketrans("0123456789", "ABCDEFGHIJ")


def _store_path() -> Path:
    override = os.environ.get("ASSISTANT_FAILURES_FILE")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME") or Path.home())
    return home / ".assistant" / "failures.json"


def fingerprint(component: str, error_text: str) -> str:
    """A stable class for "the same failure": the error with numbers, ids,
    hex runs, home paths and quoted values removed, whitespace collapsed, the
    first 100 characters kept, prefixed with the component."""
    text = str(error_text)[:2000]
    text = _ERROR_TYPE.sub(lambda m: f"type {m.group(1)}", text)
    text = _STATUS.sub(lambda m: m.group(1) + m.group(2) + "#" + m.group(3).translate(_DIGIT_LETTERS), text)
    for pattern in (_REQUEST_ID, _QUOTED, _UUID, _HOME_PATH, _ABS_PATH, _HEX, _DIGITS):
        text = pattern.sub("", text)
    text = _SPACE.sub(" ", text).strip()[:FINGERPRINT_TEXT]
    return f"{component}: {text}"


def _sample(error_text: str) -> str:
    """The error as shown to a person: one line, secrets replaced, at most 200
    characters. Redacted before it is cut, so a token can't straddle the cut."""
    text = _SPACE.sub(" ", str(error_text)[:2000]).strip()
    return _SECRETS.sub("<redacted>", text)[:MAX_SAMPLE]


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


@contextlib.contextmanager
def _locked(path: Path):
    """An exclusive lock beside the store. Gives up after a couple of seconds
    rather than hold up a pulse; the caller's guard then drops the record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _write(path: Path, data: dict) -> None:
    """Temp file then replace, so a crash in between leaves the old file whole."""
    if len(data) > MAX_FINGERPRINTS:
        keep = sorted(data, key=lambda k: data[k].get("last_ts", 0), reverse=True)[:MAX_FINGERPRINTS]
        data = {k: data[k] for k in keep}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _pause_sec(streak: int) -> int:
    if streak < BACKOFF_AFTER:
        return 0
    return min(BACKOFF_BASE_SEC * 2 ** (streak - BACKOFF_AFTER), BACKOFF_CAP_SEC)


def record_failure(component: str, error_text: str, now: float | None = None,
                   pause: bool = True, cap: int | None = None,
                   context: str | None = None) -> dict | None:
    """Count one failure. Returns the updated row, or None if it couldn't be saved.
    `pause=False` is for work nobody can hold back (a person's own request, or
    something that is not a retry): it is counted and shown but never paused,
    so the dashboard never says retries are paused when they are not.
    `cap` shortens the longest pause (seconds). `context` names what the job
    was failing on (a remote and branch, say); when it changes the streak
    starts over, and `backed_off(..., context=)` ignores a pause set for
    another one."""
    try:
        now = time.time() if now is None else now
        path = _store_path()
        key = fingerprint(component, error_text)
        cap = BACKOFF_CAP_SEC if cap is None else min(cap, BACKOFF_CAP_SEC)
        with _locked(path):
            data = _read(path)
            for other, row in data.items():
                if row.get("component") == component and other != key:
                    _reset(row)  # a different error starts over
            row = data.get(key) or {"component": component, "count": 0, "streak": 0, "first_ts": now}
            if context is not None and row.get("context") not in (None, context):
                _reset(row)  # it is failing on something else now
            if not row.get("streak"):
                row["streak_first_ts"] = now
            row["count"] = int(row.get("count") or 0) + 1
            row["streak"] = int(row.get("streak") or 0) + 1
            row["last_ts"] = now
            row["sample"] = _sample(error_text)
            row["context"] = context
            row["cap"] = cap
            wait = min(_pause_sec(row["streak"]), cap) if pause else 0
            row["backoff_until"] = now + wait if wait else 0
            data[key] = row
            _write(path, data)
        return row
    except Exception:  # noqa: BLE001 — failwatch must never break its caller
        return None


def _reset(row: dict) -> None:
    row["streak"], row["backoff_until"], row["streak_first_ts"] = 0, 0, 0


def record_ok(component: str, now: float | None = None) -> None:
    """The operation worked: clear the streak and pause, keep the history."""
    try:
        path = _store_path()
        if not path.exists():  # the common case: nothing has ever failed
            return
        with _locked(path):
            data = _read(path)
            changed = False
            for row in data.values():
                if row.get("component") == component and (row.get("streak") or row.get("backoff_until")):
                    _reset(row)
                    changed = True
            if changed:
                _write(path, data)
    except Exception:  # noqa: BLE001
        return


def pause_in_force(row: dict, now: float, context: str | None = None) -> float:
    """When this row's pause ends, or 0 if none is in force. A pause further
    away than its own cap is a clock that jumped, not a real pause. A pause set
    for another context no longer applies."""
    until = float(row.get("backoff_until") or 0)
    cap = float(row.get("cap") or BACKOFF_CAP_SEC)
    if until <= now or until > now + cap:
        return 0.0
    if context is not None and row.get("context") not in (None, context):
        return 0.0
    return until


def backed_off(component: str, now: float | None = None, context: str | None = None) -> float | None:
    """The time until which `component` should not retry, or None. Pass
    `context` (what the job would try now) so a pause that was set for
    something else, such as a branch that has since changed, doesn't hold."""
    try:
        now = time.time() if now is None else now
        until = max((pause_in_force(row, now, context)
                     for row in _read(_store_path()).values() if row.get("component") == component),
                    default=0.0)
        return until or None
    except Exception:  # noqa: BLE001
        return None


def record_ok_except(prefix: str, live: set[str], now: float | None = None) -> None:
    """Clear every row whose component is `prefix` plus a name not in `live`:
    the thing it was about is gone (a closed session)."""
    try:
        path = _store_path()
        if not path.exists():
            return
        with _locked(path):
            data = _read(path)
            changed = False
            for row in data.values():
                component = str(row.get("component", ""))
                if component.startswith(prefix) and component[len(prefix):] not in live and row.get("streak"):
                    _reset(row)
                    changed = True
            if changed:
                _write(path, data)
    except Exception:  # noqa: BLE001
        return


def history() -> list[dict]:
    """Every failure on record, most recent first, including ones that have
    since recovered. `repeated()` is the subset still happening."""
    try:
        return sorted(_read(_store_path()).values(), key=lambda r: -float(r.get("last_ts") or 0))
    except Exception:  # noqa: BLE001
        return []


def repeated(now: float | None = None, min_count: int = 3, window_sec: int = 86400,
             limit: int = MAX_SHOWN) -> list[dict]:
    """The failures happening now, most in a row first, at most `limit`. A row
    is listed when its current streak is at least `min_count` and its last
    failure is inside the window, or while its pause is still in force
    whatever the window says. A failure that has since worked, or was replaced
    by a different error, has a streak of 0 and is not listed: the dashboard
    must not report something that recovered. Each row has `component`,
    `count` (all time), `streak` (in a row now), `first_ts`, `streak_first_ts`
    (when this run began), `last_ts`, `sample`, `backoff_until`, `context`."""
    try:
        now = time.time() if now is None else now
        rows = []
        for row in _read(_store_path()).values():
            streak = int(row.get("streak") or 0)
            recent = float(row.get("last_ts") or 0) >= now - window_sec
            if (streak >= min_count and recent) or (streak and pause_in_force(row, now)):
                rows.append(row)
        rows.sort(key=lambda r: (-int(r["streak"]), -float(r["last_ts"])))
        return rows[:limit]
    except Exception:  # noqa: BLE001
        return []
