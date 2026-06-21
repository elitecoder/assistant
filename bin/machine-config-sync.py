#!/usr/bin/env python3
"""machine-config-sync — keep the machine-config repo and this box in sync.

Full-auto, driven hourly by com.assistant.machine-config-sync LaunchAgent:
  1. sync-push.sh  — capture local config drift, commit + push (no-op when clean)
  2. sync-pull.sh  — pull other machines' changes, re-project onto this box

Mirrors bin/memory-sync-pull.py: throttled via a last-run marker, logs only when
something actually moved, and sets MACHINE_CONFIG_SYNC_IN_PROGRESS to guard
against any sync loop.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG_REPO = HOME / "dev" / "machine-config"
SYNC_PUSH = CONFIG_REPO / "scripts" / "sync-push.sh"
SYNC_PULL = CONFIG_REPO / "scripts" / "sync-pull.sh"
LAST_RUN_PATH = HOME / ".assistant" / "machine-config-sync-last.json"
DEFAULT_INTERVAL = 3600


def main() -> int:
    if not CONFIG_REPO.exists():
        print(f"machine-config repo not found at {CONFIG_REPO} — skipping", file=sys.stderr)
        return 0  # not an error: machine may not have opted in yet

    now = time.time()
    if LAST_RUN_PATH.exists():
        try:
            last = json.loads(LAST_RUN_PATH.read_text()).get("ts", 0)
            if now - last < DEFAULT_INTERVAL:
                return 0
        except Exception:
            pass

    env = {**os.environ, "MACHINE_CONFIG_SYNC_IN_PROGRESS": "1"}
    pushed = pulled = False
    rc = 0

    if SYNC_PUSH.exists():
        r = subprocess.run(["bash", str(SYNC_PUSH)], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            rc = r.returncode
        elif "Nothing to push" not in r.stdout:
            pushed = True
            print(r.stdout.strip())

    if SYNC_PULL.exists():
        r = subprocess.run(["bash", str(SYNC_PULL)], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            rc = rc or r.returncode
        elif "Fast-forward" in r.stdout or "Updating" in r.stdout:
            pulled = True
            print("machine-config: pulled config changes from another machine.")

    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(json.dumps({"ts": now, "rc": rc, "pushed": pushed, "pulled": pulled}))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
