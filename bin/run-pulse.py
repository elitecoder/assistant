#!/usr/bin/env python3
"""Launchd pre-flight for the Assistant pulse.

Parses the files the pulse loads at startup, clears any earlier failure record,
then replaces this process with bin/pulse.py. If one won't parse, it skips the
run and exits 0 instead of starting a pulse that would crash on import:

  - ~/.assistant/pulse-preflight.json holds the error; the dashboard shows it
    at the top of the page until a pulse runs again.
  - An actions-ledger entry (so Slack hears about it) is written for a new
    error, then once a day while it lasts.
  - stderr (the LaunchAgent's err log) gets one line per skipped run.

The LaunchAgent fires every StartInterval whatever the exit code, so the next
tick retries. Modules the pulse loads later aren't checked here: it guards
those imports itself, and self_update.py refuses incoming commits that don't
parse.
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
LEDGER_EVERY_SEC = 86400  # 1 day


def _record_failure(assistant_dir: Path, error: str, now: float) -> None:
    record = assistant_dir / "pulse-preflight.json"
    try:
        previous = json.loads(record.read_text())
    except (OSError, ValueError):
        previous = {}
    ledgered_at = previous.get("ledgered_at") if previous.get("error") == error else None
    try:
        assistant_dir.mkdir(parents=True, exist_ok=True)
        if ledgered_at is None or now - ledgered_at >= LEDGER_EVERY_SEC:
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(assistant_dir / "actions-ledger.jsonl", "a") as ledger:
                ledger.write(json.dumps({
                    "ts": stamp, "epoch": int(now), "key": f"pulse-preflight-fail-{int(now)}",
                    "kind": "pulse-preflight-fail", "ws_ref": "(launchd)", "outcome": "failed",
                    "evidence": f"pulse can't start: {error}"[:300],
                }) + "\n")
            ledgered_at = now
        tmp = record.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"failed_at": now, "error": error, "ledgered_at": ledgered_at}))
        tmp.replace(record)
    except OSError as exc:
        print(f"pulse pre-flight could not record the failure in {assistant_dir}: {exc}",
              file=sys.stderr)


def main(argv: list[str], *, execv=os.execv) -> int:
    assistant_dir = Path.home() / ".assistant"
    for path in STARTUP_FILES:
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, ValueError) as exc:
            error = f"{path}: {type(exc).__name__}: {exc}"
            now = time.time()
            _record_failure(assistant_dir, error, now)
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"[{stamp}] pulse pre-flight FAILED, skipping this run: {error}",
                  file=sys.stderr)
            return 0
    (assistant_dir / "pulse-preflight.json").unlink(missing_ok=True)
    execv(sys.executable, [sys.executable, str(PULSE), *argv])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
