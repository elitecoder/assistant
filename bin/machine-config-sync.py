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
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG_REPO = HOME / "dev" / "machine-config"
SYNC_PUSH = CONFIG_REPO / "scripts" / "sync-push.sh"
SYNC_PULL = CONFIG_REPO / "scripts" / "sync-pull.sh"
LAST_RUN_PATH = HOME / ".assistant" / "machine-config-sync-last.json"
# The opt-in marker install.sh step 8 writes ONLY after an explicit interactive
# opt-in. The RUNTIME gates on it (below), which is the real opt-in enforcement:
# macOS launchd bootstraps every ~/Library/LaunchAgents plist at login
# regardless of `launchctl load`, so the copied-but-not-loaded plist WILL
# auto-load at the next login — and RunAtLoad would fire this daemon on any box
# where ~/dev/machine-config already exists (it does, pre-PR). Gating only in
# install.sh (load time) is therefore decorative; the wrapper must refuse to
# sync until the marker exists (review BLOCKER).
CONFIGURED_MARKER = HOME / ".assistant" / "machine-config-configured"
# Below the plist's StartInterval (3600) so launchd's timer jitter — a fire a
# few seconds early — can't trip `now - last < INTERVAL` and skip the cycle,
# which would drift the effective cadence toward 2h (review). The throttle only
# guards against a manual run racing the timer; the hourly fires always pass.
DEFAULT_INTERVAL = 1800


def _env_timeout() -> int:
    """Per-script timeout from MACHINE_CONFIG_SYNC_TIMEOUT, never crashing on a
    bad value (a non-numeric env would ValueError-traceback every hour and never
    write the marker) — falls back to 300s."""
    raw = os.environ.get("MACHINE_CONFIG_SYNC_TIMEOUT", "300")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        print(f"machine-config: bad MACHINE_CONFIG_SYNC_TIMEOUT={raw!r} → 300s",
              file=sys.stderr)
        return 300


def _run_script(path: Path, label: str, env: dict,
                timeout_s: int) -> tuple[int, str, str, bool]:
    """Run one sync script, killing the WHOLE process group on timeout. A plain
    subprocess.run(timeout=) SIGKILLs only the direct `bash` child and ORPHANS
    the hung `git`/`ssh` grandchild — the exact process the timeout exists to
    stop — which could then hold .git/index.lock while the next script runs
    (review). start_new_session + os.killpg reaps the whole tree. stdin is
    /dev/null so an SSH host-key/passphrase prompt aborts rather than blocks.
    Returns (rc, stdout, stderr, timed_out)."""
    try:
        proc = subprocess.Popen(
            ["bash", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
    except OSError as e:
        return 127, "", f"spawn failed: {e}", False
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out, err = "", ""
        print(f"machine-config: {label} timed out after {timeout_s}s "
              f"(process group killed)", file=sys.stderr)
        return 124, out or "", err or "", True


def main() -> int:
    # Loop guard: sync-push/sync-pull (and any git hook they trigger) inherit
    # MACHINE_CONFIG_SYNC_IN_PROGRESS=1. If we're already inside a sync, bail
    # before doing anything so a re-entrant invocation can't recurse.
    if os.environ.get("MACHINE_CONFIG_SYNC_IN_PROGRESS") == "1":
        return 0

    # OPT-IN GATE (the real one). Refuse to sync unless the operator explicitly
    # opted this box in via install.sh step 8. This holds even when launchd
    # auto-loads the plist at login, so a config-pushing daemon never runs
    # itself onto an un-opted-in box (see CONFIGURED_MARKER note above).
    if not CONFIGURED_MARKER.exists():
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

    # GIT_TERMINAL_PROMPT=0 so a credential prompt aborts instead of blocking; a
    # per-script timeout with process-group kill so a hung git-over-SSH can't
    # wedge the daemon (launchd won't start a second instance of a running
    # label, so a hang would stop sync permanently). Both were absent (review).
    env = {**os.environ, "MACHINE_CONFIG_SYNC_IN_PROGRESS": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    timeout_s = _env_timeout()
    pushed = pulled = False

    prc, pout, perr, _ptimed = _run_script(SYNC_PUSH, "sync-push", env, timeout_s)
    if prc != 0:
        if perr:
            print(perr, file=sys.stderr)
    elif "Nothing to push" not in pout:
        pushed = True
        print(pout.strip())
    rc = prc

    # SKIP pull when push failed or timed out. A rejected non-fast-forward push
    # means the remote moved; a killed push may have left a partial state /
    # index.lock. Pulling/re-projecting onto that risks an unattended merge over
    # live-symlinked config — better to retry the whole cycle next hour. This is
    # also the repo's own rule ("local commits ahead ALWAYS block the pull").
    if prc == 0:
        lrc, lout, lerr, _ltimed = _run_script(SYNC_PULL, "sync-pull", env, timeout_s)
        if lrc != 0:
            if lerr:
                print(lerr, file=sys.stderr)
            rc = lrc
        elif "Fast-forward" in lout or "Updating" in lout:
            pulled = True
            print("machine-config: pulled config changes from another machine.")
    else:
        print("machine-config: push failed — skipping pull, retrying next cycle",
              file=sys.stderr)

    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(json.dumps({"ts": now, "rc": rc, "pushed": pushed, "pulled": pulled}))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
