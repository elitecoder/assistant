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
from pathlib import Path

DEFAULT_CAP_MB = 20.0
DEFAULT_KEEP_MB = 5.0
_MB = 1024 * 1024
_LOG_SUFFIXES = (".log", ".out", ".err")


def _megabytes(value, default: float) -> float:
    """A finite number >= 0, else the default. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return float(value)


def limits(assistant_dir: Path | str) -> tuple[int, int]:
    """(cap, keep) in bytes from config.json. A cap of 0 means off.

    Anything missing, malformed or out of range falls back to the default."""
    try:
        data = json.loads((Path(assistant_dir) / "config.json").read_text())
        section = data.get("logs") if isinstance(data, dict) else None
    except (OSError, ValueError):
        section = None
    if not isinstance(section, dict):
        section = {}
    cap = int(_megabytes(section.get("cap_mb"), DEFAULT_CAP_MB) * _MB)
    keep = int(_megabytes(section.get("keep_mb"), DEFAULT_KEEP_MB) * _MB)
    return cap, min(keep, cap)


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


def cap_logs(assistant_dir: Path | str, claude_dir: Path | str) -> dict:
    """Cap every Assistant log over the limit. Never raises.

    Returns {"capped": [names], "freed": bytes, "keep": bytes}. Nothing is
    capped when the cap is 0."""
    summary: dict = {"capped": [], "freed": 0, "keep": 0}
    try:
        cap, keep = limits(assistant_dir)
        summary["keep"] = keep
        if cap <= 0:
            return summary
        for path in _candidates(Path(assistant_dir), Path(claude_dir)):
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
