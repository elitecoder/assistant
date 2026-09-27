#!/usr/bin/env python3
"""Launchd pre-flight for the Assistant pulse.

Parses bin/pulse.py, then replaces this process with it. If pulse.py won't
parse, it prints one line to stderr (the LaunchAgent sends stderr to
~/.assistant/logs/assistant-pulse.launchd.err) and exits 0 instead of starting a
pulse that would crash on its first line. The LaunchAgent fires every
StartInterval whatever the exit code, so the next tick retries. Other modules
aren't checked here: the pulse guards its own optional imports, and
self_update.py refuses incoming commits that don't parse.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PULSE = Path(__file__).resolve().parent / "pulse.py"


def main(argv: list[str], *, execv=os.execv) -> int:
    try:
        compile(PULSE.read_bytes(), str(PULSE), "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{stamp}] pulse pre-flight FAILED, skipping this run: "
              f"{PULSE}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    execv(sys.executable, [sys.executable, str(PULSE), *argv])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
