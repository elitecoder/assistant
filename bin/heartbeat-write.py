#!/usr/bin/env python3
"""heartbeat-write.py — atomically write ~/.assistant/heartbeat.json.

Mechanical only. The Assistant prompt decides WHAT status to write
(active / idle / respawn-requested / stale_world). This script just
ensures the write is atomic (tmpfile + rename).

Usage:
  heartbeat-write.py --ws WS --surface SURF [--status STATUS]
                     [--pulse-count N] [--respawn] [--note TEXT]
"""
import argparse
import datetime
import json
import os
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ws", required=True)
    p.add_argument("--surface", default="")
    p.add_argument("--status", default="active")
    p.add_argument("--pulse-count", type=int, default=0)
    p.add_argument("--respawn", action="store_true",
                   help="Back-date last_pulse_ts by 700s so the watchdog respawns next tick")
    p.add_argument("--note", default="")
    args = p.parse_args()

    now = datetime.datetime.now(datetime.UTC)
    last_ts = int(now.timestamp()) - (700 if args.respawn else 0)

    hb = {
        "ws_ref": args.ws or None,
        "surface_ref": args.surface or None,
        "last_pulse_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_pulse_ts": last_ts,
        "pulses_drained_this_run": args.pulse_count,
        "status": args.status,
        "model": "sonnet-4-6-1m",
    }
    if args.note:
        hb["_note"] = args.note

    path = os.path.expanduser("~/.assistant/heartbeat.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(hb, f, indent=2)
    os.replace(tmp, path)
    print(f"wrote heartbeat status={args.status} ws={args.ws}", file=sys.stderr)


if __name__ == "__main__":
    main()
