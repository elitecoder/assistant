#!/usr/bin/env python3
"""system-health — pulse liveness at a glance.

Reads ~/.assistant/heartbeat.json for pulse_idx + freshness, and shells out to
`launchctl list` to confirm the pulse LaunchAgent is loaded and grab its PID.

"fresh" vs "stale" uses the same 600s threshold comms_lib defaults to for the
heartbeat-stale page, so this tool and the pager agree on what "stale" means.

Returns JSON to stdout:
  {"pulse_idx": 1413, "heartbeat_age_seconds": 35, "heartbeat_status": "fresh",
   "launchd_loaded": true, "launchd_pid": 12345}

launchd_pid is null when the agent is loaded but not currently executing
(launchctl shows "-" between scheduled runs — that's normal for an interval
agent, not a fault).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
HEARTBEAT_PATH = HOME / ".assistant" / "heartbeat.json"
PULSE_LABEL = "com.assistant.assistant-pulse"

# Match comms_lib.Config.stale_heartbeat_sec default so "stale" means the same
# thing here as in the heartbeat pager.
STALE_THRESHOLD_SEC = 600


def _load_heartbeat() -> dict[str, Any]:
    try:
        data = json.loads(HEARTBEAT_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _launchd_status(label: str = PULSE_LABEL) -> tuple[bool, int | None]:
    """Return (loaded, pid). pid is None when loaded-but-not-running.

    `launchctl list` prints `PID  STATUS  LABEL` lines; a loaded-but-idle agent
    shows PID as "-". A missing label means not loaded."""
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True,
                           text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False, None
    if p.returncode != 0:
        return False, None
    for line in p.stdout.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if not parts or parts[-1] != label:
            continue
        pid_raw = parts[0].strip()
        if pid_raw in ("-", ""):
            return True, None
        try:
            return True, int(pid_raw)
        except ValueError:
            return True, None
    return False, None


def system_health() -> dict[str, Any]:
    hb = _load_heartbeat()
    last_ts = hb.get("last_pulse_ts")
    age = int(time.time() - last_ts) if isinstance(last_ts, (int, float)) else None
    if age is None:
        status = "unknown"
    elif age <= STALE_THRESHOLD_SEC:
        status = "fresh"
    else:
        status = "stale"
    loaded, pid = _launchd_status()
    return {
        "pulse_idx": hb.get("pulse_idx"),
        "heartbeat_age_seconds": age,
        "heartbeat_status": status,
        "launchd_loaded": loaded,
        "launchd_pid": pid,
    }


def main() -> int:
    argparse.ArgumentParser(
        description="Heartbeat age, pulse index, and launchd status for the "
                    "assistant-pulse agent. Returns JSON.").parse_args()
    print(json.dumps(system_health()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
