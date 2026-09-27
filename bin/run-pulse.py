#!/usr/bin/env python3
"""Launchd pre-flight for the Assistant pulse.

Parses bin/pulse.py and the src/ package it imports, then replaces this process
with the pulse. If any of them won't parse, it prints one line to stderr (the
LaunchAgent sends stderr to ~/.assistant/logs/assistant-pulse.launchd.err) and
exits 0 instead of starting a pulse that would crash on import. The LaunchAgent
fires every StartInterval whatever the exit code, so the next tick retries.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent
PULSE = BIN / "pulse.py"
SRC = BIN.parent / "src"


def first_parse_error(paths: list[Path]) -> str | None:
    for path in paths:
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            return f"{path}: {type(exc).__name__}: {exc}"
    return None


def main(argv: list[str], *, execv=os.execv) -> int:
    error = first_parse_error([PULSE, *sorted(SRC.rglob("*.py"))])
    if error:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{stamp}] pulse pre-flight FAILED, skipping this run: {error}",
              file=sys.stderr)
        return 0
    execv(sys.executable, [sys.executable, str(PULSE), *argv])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
