#!/usr/bin/env python3
"""
Layer 2: Append-only session ledger.

Fires on SessionStart and Stop (via the event arg in argv[1]).
Appends one JSON line to ~/.cmux-session-ledger.jsonl per event so that
cmux-restore-sessions can reconstruct the session→workspace mapping even
if cmux state is wiped.

SessionStart line:
  {"event":"start","ts":..., "session_id":"...", "cwd":"...", "transcript_path":"...",
   "ws_title":"...", "panel_id":"...", "workspace_id":"...", "pid":...}

Stop/SessionEnd line:
  {"event":"end","ts":..., "session_id":"...", "pid":...}
"""
import fcntl
import json
import os
import sys
import time

LEDGER = os.path.expanduser("~/.cmux-session-ledger.jsonl")
LOCK = LEDGER + ".lock"


def append_line(entry: dict):
    os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
    with open(LOCK, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "start"

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    sid = payload.get("session_id", "")
    if not sid:
        return

    ts = time.time()
    pid = int(os.environ.get("CMUX_CLAUDE_PID", 0))

    if event == "start":
        # Get workspace title from cmux if possible
        ws_title = ""
        try:
            import subprocess
            result = subprocess.run(
                ["cmux", "identify", "--json"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                ws_title = info.get("caller", {}).get("workspace", {}).get("title", "")
        except Exception:
            pass

        entry = {
            "event": "start",
            "ts": ts,
            "session_id": sid,
            "cwd": payload.get("cwd", os.getcwd()),
            "transcript_path": payload.get("transcript_path", ""),
            "ws_title": ws_title,
            "panel_id": os.environ.get("CMUX_PANEL_ID", ""),
            "surface_id": os.environ.get("CMUX_SURFACE_ID", ""),
            "workspace_id": os.environ.get("CMUX_WORKSPACE_ID", ""),
            "pid": pid,
        }
    else:
        entry = {
            "event": "end",
            "ts": ts,
            "session_id": sid,
            "pid": pid,
        }

    append_line(entry)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
