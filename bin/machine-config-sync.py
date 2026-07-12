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
    # Loop guard: sync-push/sync-pull (and any git hook they trigger) inherit
    # MACHINE_CONFIG_SYNC_IN_PROGRESS=1. If we're already inside a sync, bail
    # before doing anything so a re-entrant invocation can't recurse.
    if os.environ.get("MACHINE_CONFIG_SYNC_IN_PROGRESS") == "1":
        return 0

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

    # The repo is present, so the sync scripts MUST be too. If they're missing
    # (a config-repo restructure renamed/moved scripts/), FAIL LOUD and do NOT
    # write the throttle marker — otherwise every box silently stops syncing,
    # rc=0, logging nothing, forever. The mirrored memory-sync-pull fails loud on
    # its missing script; this restores that safety (review).
    if not SYNC_PUSH.exists() or not SYNC_PULL.exists():
        print(f"machine-config: sync scripts missing under {CONFIG_REPO / 'scripts'} "
              f"— repo present but not syncable; check the config repo", file=sys.stderr)
        return 1

    # GIT_TERMINAL_PROMPT=0 so a credential/host-key prompt aborts instead of
    # blocking; a per-script timeout so a hung git-over-SSH can't wedge the daemon
    # forever (launchd won't start a second instance of a running label, so a
    # hang would stop sync permanently). Both were absent (review).
    env = {**os.environ, "MACHINE_CONFIG_SYNC_IN_PROGRESS": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    timeout_s = int(os.environ.get("MACHINE_CONFIG_SYNC_TIMEOUT", "300"))
    pushed = pulled = False
    rc = 0

    try:
        r = subprocess.run(["bash", str(SYNC_PUSH)], capture_output=True,
                           text=True, env=env, timeout=timeout_s)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            rc = r.returncode
        elif "Nothing to push" not in r.stdout:
            pushed = True
            print(r.stdout.strip())
    except subprocess.TimeoutExpired:
        print(f"machine-config: sync-push timed out after {timeout_s}s", file=sys.stderr)
        rc = 124

    try:
        r = subprocess.run(["bash", str(SYNC_PULL)], capture_output=True,
                           text=True, env=env, timeout=timeout_s)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            rc = rc or r.returncode
        elif "Fast-forward" in r.stdout or "Updating" in r.stdout:
            pulled = True
            print("machine-config: pulled config changes from another machine.")
    except subprocess.TimeoutExpired:
        print(f"machine-config: sync-pull timed out after {timeout_s}s", file=sys.stderr)
        rc = rc or 124

    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(json.dumps({"ts": now, "rc": rc, "pushed": pushed, "pulled": pulled}))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
