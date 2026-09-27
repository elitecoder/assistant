#!/usr/bin/env python3
"""Launchd pre-flight for the Assistant pulse.

Parses the files the pulse loads at startup, then replaces this process with
bin/pulse.py. If one won't parse, it records the error in
~/.assistant/pulse-preflight.json (the dashboard's pulse banner shows it), prints
it to stderr (the LaunchAgent's err log), and exits 0 instead of starting a
pulse that would crash on import. The LaunchAgent fires every StartInterval
whatever the exit code, so the next tick retries. Modules the pulse loads later
aren't checked here: it guards those imports itself, and self_update.py refuses
incoming commits that don't parse.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent
PULSE = BIN / "pulse.py"
# pulse.py plus its unguarded module-level imports. Keep in sync with pulse.py.
STARTUP_FILES = (PULSE, BIN.parent / "src/assistant/__init__.py",
                 BIN.parent / "src/assistant/model_tiers.py")


def main(argv: list[str], *, execv=os.execv) -> int:
    for path in STARTUP_FILES:
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            error = f"{path}: {type(exc).__name__}: {exc}"
            record = Path.home() / ".assistant/pulse-preflight.json"
            record.parent.mkdir(parents=True, exist_ok=True)
            tmp = record.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"failed_at": time.time(), "error": error}))
            tmp.replace(record)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"[{stamp}] pulse pre-flight FAILED, skipping this run: {error}",
                  file=sys.stderr)
            return 0
    execv(sys.executable, [sys.executable, str(PULSE), *argv])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
