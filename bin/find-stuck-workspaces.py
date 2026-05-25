#!/usr/bin/env python3
"""find-stuck-workspaces.py — list workspaces whose observer state has been
unchanged for more than the configured threshold (default 2h).

Mechanical only. The Assistant prompt decides what to do (escalate to Opus
sub-agent, surface awaiting card, etc.). This script just enumerates.

Output: JSON array of {ws_ref, title, cwd, classification, stuck_for_sec,
                       state_hash, summary, pr_refs}.

Usage:
  find-stuck-workspaces.py [--threshold-sec 7200]
"""
import argparse
import glob
import json
import os
import time

SUMM_DIR = os.path.expanduser("~/.assistant/observer-summaries")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--threshold-sec", type=int, default=7200,
                   help="Stuck-for threshold in seconds (default 7200 = 2h)")
    args = p.parse_args()

    now = int(time.time())
    stuck = []
    for f in glob.glob(os.path.join(SUMM_DIR, "workspace_*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        since = d.get("state_unchanged_since_ts")
        if not since:
            continue
        stuck_for = now - int(since)
        if stuck_for < args.threshold_sec:
            continue
        stuck.append({
            "ws_ref": d.get("ws_ref"),
            "title": d.get("title", ""),
            "cwd": d.get("cwd", ""),
            "classification": d.get("classification", ""),
            "stuck_for_sec": stuck_for,
            "state_hash": d.get("state_hash", ""),
            "summary": (d.get("summary_for_next_pulse") or "")[:200],
            "pr_refs": d.get("pr_refs", []),
        })
    stuck.sort(key=lambda x: -x["stuck_for_sec"])  # most-stuck first
    print(json.dumps(stuck, indent=2))


if __name__ == "__main__":
    main()
