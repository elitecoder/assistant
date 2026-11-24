"""Cap the Assistant's log files so none grows without bound.

launchd holds each service's stdout and stderr open in append mode and nothing
rotates them. An over-limit file keeps its last `keep` bytes (from a line
boundary) in `<name>.1`, then is shrunk in place: the writer's descriptor stays
valid, so nothing has to restart.

Bytes the writer appends between our read and our truncate are lost. That is
fine for a diagnostics log. The file is never left corrupted: an append-mode
write always lands at the current end, whatever the truncate did.

Stdlib only, Python 3.11 or older features only (launchd services import it).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_CAP_MB = 20.0
DEFAULT_KEEP_MB = 5.0
CAP_ENV = "ASSISTANT_LOG_CAP_MB"
KEEP_ENV = "ASSISTANT_LOG_KEEP_MB"
_MB = 1024 * 1024
_LOG_SUFFIXES = (".log", ".out", ".err")


def _megabytes(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def limits() -> tuple[int, int]:
    """(cap, keep) in bytes from the environment. A cap of 0 means off."""
    cap = int(_megabytes(CAP_ENV, DEFAULT_CAP_MB) * _MB)
    keep = int(_megabytes(KEEP_ENV, DEFAULT_KEEP_MB) * _MB)
    return cap, min(keep, cap)


def _is_log_name(name: str) -> bool:
    return name.endswith(_LOG_SUFFIXES)


def _candidates(assistant_dir: Path, claude_dir: Path) -> list[Path]:
    found: list[Path] = []
    for directory, only_log_ext in ((assistant_dir / "logs", False), (assistant_dir, True)):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if _is_log_name(name) and (not only_log_ext or name.endswith(".log")):
                found.append(directory / name)
    found.append(assistant_dir / "comms" / "comms-listen.log")
    found.append(claude_dir / "logs" / "workspace-watcher.log")
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
        start = max(size - keep, 0)
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        tail = b"".join(chunks)
        # The cut falls inside a line (keep <= cap < size, so start > 0); drop it.
        newline = tail.find(b"\n")
        tail = tail[newline + 1:] if newline != -1 else b""
        archive = path.with_name(path.name + ".1")
        tmp = path.with_name(path.name + ".1.tmp")
        tmp_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                out.write(tail)
            os.replace(tmp, archive)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # Shrink in place, keeping the newest bytes in the file itself: write
        # the tail over the front, then cut. The writer's append-mode
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

    Returns {"capped": [names], "freed": bytes}. Nothing is capped when the
    cap is 0."""
    summary: dict = {"capped": [], "freed": 0}
    try:
        cap, keep = limits()
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
