"""The per-install token that lets a caller outside the dashboard page change things.

`~/.assistant/api-token` holds 32 random bytes as URL-safe text, readable by its owner only. The dashboard
server asks for it from any request that doesn't come from the page it serves (a command-line call, or the
dashboard opened as a local file), and the page renderer embeds it in the page it writes. One lock covers the
read and the write, so two processes starting at once end up with one token.
"""
from __future__ import annotations

import fcntl
import os
import secrets
from pathlib import Path

# 32 random bytes are 43 characters; anything shorter was typed by hand and is not accepted.
MIN_LENGTH = 32


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def path() -> Path:
    # Per call, not at import, so a test or a service that sets HOME later still reads the right file.
    return _home() / ".assistant" / "api-token"


def _read(file: Path) -> str:
    try:
        return file.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def ensure() -> str:
    """Return the token, writing a new one first when the file is missing, empty, too short or unreadable."""
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    with open(file.with_name("api-token.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _read(file)
        if len(token) >= MIN_LENGTH:
            os.chmod(file, 0o600)  # a file made too open by hand is tightened, not trusted as it is
            return token
        token = secrets.token_urlsafe(32)
        temporary = file.with_name(f"api-token.{os.getpid()}.tmp")
        try:
            # Create the file private from the start: the token is never readable by others, even briefly.
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(token + "\n")
            os.replace(temporary, file)
        except BaseException:
            temporary.unlink(missing_ok=True)  # no stray file when the write or the rename fails
            raise
        return token
