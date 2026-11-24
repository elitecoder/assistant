"""Cap the Assistant's log files so none grows without bound.

launchd holds each service's stdout and stderr open in append mode and nothing
rotates them. An over-limit file keeps its last `keep` bytes (from a line
boundary) and loses everything older. It is shrunk in place: the writer's
descriptor stays valid, so nothing has to restart.

Bytes the writer appends between our read and our truncate are lost. That is
fine for a diagnostics log. The file is never left corrupted: an append-mode
write always lands at the current end, whatever the truncate did.

The limits come from ~/.assistant/config.json:
    {"logs": {"cap_mb": 20, "keep_mb": 5}}
A launchd service never sees a shell `export`, so a file is the only setting a
real user can change. `cap_mb: 0` turns trimming off.

Stdlib only, Python 3.11 or older features only (launchd services import it).
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path

DEFAULT_CAP_MB = 20.0
DEFAULT_KEEP_MB = 5.0
_MB = 1024 * 1024
_LOG_SUFFIXES = (".log", ".out", ".err")


DAY_SEC = 24 * 60 * 60
STATE_NAME = "log-cap-state.json"


def _bad_number(name: str, value) -> str | None:
    """Why a setting isn't usable, or None when it is a finite number >= 0.
    A bool is not a number here."""
    if isinstance(value, str):
        return f"logs.{name} in ~/.assistant/config.json is {json.dumps(value)} in quotes, not a number"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"logs.{name} in ~/.assistant/config.json is {json.dumps(value)}, not a number"
    if not math.isfinite(value) or value < 0:
        return f"logs.{name} in ~/.assistant/config.json is {json.dumps(value)}, not a number of 0 or more"
    return None


def settings(assistant_dir: Path | str) -> dict:
    """The limits in bytes, from config.json.

    {"cap": bytes, "keep": bytes, "explicit": bool, "problem": str | None}

    The defaults apply only when the file is absent or has no `logs` key. A
    setting that exists but can't be read means trimming is OFF (`problem`
    says why): someone who typed "0" in quotes asked for no trimming and must
    not get their logs deleted. `explicit` is True when `logs` is present."""
    default = {"cap": int(DEFAULT_CAP_MB * _MB), "keep": int(DEFAULT_KEEP_MB * _MB),
               "explicit": False, "problem": None}
    off = {**default, "cap": 0, "keep": 0}
    try:
        text = (Path(assistant_dir) / "config.json").read_text()
    except FileNotFoundError:
        return default
    except OSError as exc:
        return {**off, "problem": f"~/.assistant/config.json can't be read ({exc.strerror or type(exc).__name__})"}
    try:
        data = json.loads(text)
    except ValueError:
        return {**off, "problem": "~/.assistant/config.json isn't valid JSON"}
    if not isinstance(data, dict):
        return {**off, "problem": "~/.assistant/config.json isn't a JSON object"}
    if "logs" not in data:
        return default
    section = data["logs"]
    if not isinstance(section, dict):
        return {**off, "problem": "logs in ~/.assistant/config.json isn't an object"}
    values = {}
    for name, fallback in (("cap_mb", DEFAULT_CAP_MB), ("keep_mb", DEFAULT_KEEP_MB)):
        if name in section:
            problem = _bad_number(name, section[name])
            if problem:
                return {**off, "explicit": True, "problem": problem}
            values[name] = float(section[name])
        else:
            values[name] = fallback
    cap = int(values["cap_mb"] * _MB)
    return {"cap": cap, "keep": min(int(values["keep_mb"] * _MB), cap),
            "explicit": True, "problem": None}


def _is_log_name(name: str) -> bool:
    return name.endswith(_LOG_SUFFIXES)


def _is_real_dir(directory: Path) -> bool:
    """A directory that is not itself a symlink (os.listdir would follow one)."""
    try:
        return stat.S_ISDIR(os.lstat(directory).st_mode)
    except OSError:
        return False


def _candidates(assistant_dir: Path, claude_dir: Path) -> list[Path]:
    found: list[Path] = []
    for directory, only_log_ext in ((assistant_dir / "logs", False), (assistant_dir, True)):
        if not _is_real_dir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if _is_log_name(name) and (not only_log_ext or name.endswith(".log")):
                found.append(directory / name)
    found.append(assistant_dir / "comms" / "comms-listen.log")
    found.append(claude_dir / "logs" / "workspace-watcher.log")
    # The todo server's launchd output files.
    found.append(claude_dir / "cache" / "todo-server.log")
    found.append(claude_dir / "cache" / "todo-server.err")
    return found


def _cap_one(path: Path, cap: int, keep: int) -> int:
    """Shrink one file. Returns bytes freed, 0 if it was left alone."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size <= cap:
        return 0
    # O_NOFOLLOW: a symlink swapped in after the lstat is refused, not followed.
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return 0
        size = os.fstat(fd).st_size
        os.lseek(fd, size - keep, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        tail = b"".join(chunks)
        # The cut falls inside a line (keep <= cap < size, so the start is past
        # byte 0); drop that partial line.
        newline = tail.find(b"\n")
        tail = tail[newline + 1:] if newline != -1 else b""
        # Write the tail over the front, then cut. The writer's append-mode
        # descriptor then writes at the new end.
        with os.fdopen(os.dup(fd), "r+b") as same:
            same.seek(0)
            same.write(tail)
            same.truncate(len(tail))
        return size - len(tail)
    finally:
        os.close(fd)


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict) -> bool:
    """Atomic, owner-only. False when it can't be written."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(state, out)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _size_over(path: Path, cap: int) -> int:
    """The size of a regular file past the cap, else 0."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) and st.st_size > cap else 0


def _problem_due(state: dict, problem: str, now: float) -> bool:
    """Say a setting problem once per distinct reason per day."""
    reasons = state.get("problems")
    last = reasons.get(problem) if isinstance(reasons, dict) else None
    return not isinstance(last, (int, float)) or last > now or now - last >= DAY_SEC


def cap_logs(assistant_dir: Path | str, claude_dir: Path | str, now: float | None = None) -> dict:
    """Cap every Assistant log over the limit. Never raises.

    Returns {"capped": [names], "freed": bytes, "keep": bytes,
             "problem": str | None, "announce": bool, "notice": dict | None}.
    Nothing is trimmed when the cap is 0 or a setting can't be read (`problem`;
    `announce` is True when it is due to be logged). With no `logs` entry in
    config.json, the first run that finds a log over the limit trims nothing
    and returns a `notice`; trimming starts 24 hours after that."""
    summary: dict = {"capped": [], "freed": 0, "keep": 0, "problem": None,
                     "announce": False, "notice": None}
    try:
        now = time.time() if now is None else now
        assistant_dir = Path(assistant_dir)
        conf = settings(assistant_dir)
        cap, keep = conf["cap"], conf["keep"]
        summary["keep"] = keep
        state_path = assistant_dir / STATE_NAME
        if conf["problem"]:
            state = _read_state(state_path)
            summary["problem"] = conf["problem"]
            if _problem_due(state, conf["problem"], now):
                summary["announce"] = True
                reasons = state.get("problems") if isinstance(state.get("problems"), dict) else {}
                state["problems"] = {**reasons, conf["problem"]: now}
                _write_state(state_path, state)
            return summary
        if cap <= 0:
            return summary
        paths = _candidates(assistant_dir, Path(claude_dir))
        if not conf["explicit"]:
            over = [(p, _size_over(p, cap)) for p in paths]
            over = [(p, n) for p, n in over if n]
            if not over:
                return summary
            state = _read_state(state_path)
            first = state.get("first_over")
            usable = isinstance(first, (int, float)) and not isinstance(first, bool) and first <= now
            if not usable:
                # No record yet, or one from the future (the clock was set back).
                state["first_over"] = first = now
                recorded = _write_state(state_path, state)
            else:
                recorded = True
            if not recorded or now - first < DAY_SEC:
                summary["notice"] = {"trim_at": first + DAY_SEC, "cap": cap, "keep": keep,
                                     "saved": recorded,
                                     "names": [p.name for p, _ in over],
                                     "bytes": sum(n for _, n in over)}
                return summary
        for path in paths:
            try:
                freed = _cap_one(path, cap, keep)
            except Exception:  # noqa: BLE001 — one bad file must not stop the rest
                continue
            if freed:
                summary["capped"].append(path.name)
                summary["freed"] += freed
    except Exception:  # noqa: BLE001 — a log capper must never break its caller
        pass
    return summary
