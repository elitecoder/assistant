#!/usr/bin/env python3
"""pick-open-todos.py — enumerate dispatch candidates from assistant-todo.json.

Mechanical only. The Assistant decides what to do with each candidate (spawn,
re-dispatch, surface card). This script just walks items[], filters by
status==open, and routes each into Bucket A / B / C with the data the
prompt's Step 3.5 needs to act.

Bucket A: autoDispatch=true AND dispatchedAt set AND dispatchedWs is GONE
          (workspace closed without flipping TODO status — needs re-classify)
Bucket B: autoDispatch=true AND dispatchedAt empty (never spawned — needs spawn)
Bucket C: autoDispatch is null (user hasn't decided — surface awaiting card)

Skipped: autoDispatch=false (manual-only by design),
         autoDispatch=true AND dispatchedWs alive (already in flight),
         status != open.

Output: JSON
  {
    "bucket_a": [{"id": "td-NNN", "title": "...", "priority": "P1",
                  "dispatched_ws": "workspace:NN",
                  "dispatched_at": "2026-...", "detail_len": 234}, ...],
    "bucket_b": [{"id": "td-NNN", "title": "...", "priority": "P1",
                  "detail_len": 234}, ...],
    "bucket_c": [{"id": "td-NNN", "title": "...", "priority": "P1"}, ...],
    "skipped_in_flight": [{"id": "td-NNN", "ws": "workspace:NN"}, ...],
    "skipped_manual": [{"id": "td-NNN"}, ...],
    "totals": {"open": N, "bucket_a": N, "bucket_b": N, "bucket_c": N,
               "skipped_in_flight": N, "skipped_manual": N}
  }
"""
import json
import os
import subprocess
import sys

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
TODO_PATH = os.path.expanduser("~/.claude/assistant-todo.json")
WORLD_PATH = os.path.expanduser("~/.claude/cache/world.json")


def live_workspaces():
    """Return set of ws_refs that have an active claude session.

    Uses world.json's live_sessions[] (canonical "is there a live session here"
    signal), with cmux list-workspaces as a fallback. The cmux list returns
    every window's tabs including hidden/stale entries, so it overcounts for
    in-flight detection.
    """
    refs = set()
    try:
        w = json.load(open(WORLD_PATH))
        for s in w.get("live_sessions", []):
            r = s.get("ws_ref")
            if r:
                refs.add(r)
        if refs:
            return refs
    except Exception:
        pass
    try:
        out = subprocess.check_output([CMUX, "list-workspaces", "--json"], text=True)
        data = json.loads(out)
        items = data if isinstance(data, list) else data.get("workspaces", [])
        return {w["ref"] for w in items if w.get("ref")}
    except Exception:
        return set()


def main():
    if not os.path.exists(TODO_PATH):
        print(json.dumps({"error": f"todo file missing: {TODO_PATH}"}))
        sys.exit(1)
    todo = json.load(open(TODO_PATH))
    live = live_workspaces()

    bucket_a = []
    bucket_b = []
    bucket_c = []
    skipped_in_flight = []
    skipped_manual = []

    for item in todo.get("items", []):
        if item.get("status") != "open":
            continue
        tid = item.get("id")
        ad = item.get("autoDispatch")
        dws = item.get("dispatched_ws") or item.get("dispatchedWs")
        dat = item.get("dispatched_at") or item.get("dispatchedAt")
        title = (item.get("title") or "")[:80]
        priority = item.get("priority") or ""
        detail_len = len(item.get("detail") or "")

        if ad is None:
            bucket_c.append({"id": tid, "title": title, "priority": priority})
            continue
        if ad is False:
            skipped_manual.append({"id": tid})
            continue
        # autoDispatch is True
        if dat and dws:
            if dws in live:
                skipped_in_flight.append({"id": tid, "ws": dws})
            else:
                bucket_a.append({
                    "id": tid, "title": title, "priority": priority,
                    "dispatched_ws": dws, "dispatched_at": dat,
                    "detail_len": detail_len,
                })
        else:
            bucket_b.append({
                "id": tid, "title": title, "priority": priority,
                "detail_len": detail_len,
            })

    out = {
        "bucket_a": bucket_a,
        "bucket_b": bucket_b,
        "bucket_c": bucket_c,
        "skipped_in_flight": skipped_in_flight,
        "skipped_manual": skipped_manual,
        "totals": {
            "open": sum(1 for i in todo.get("items", []) if i.get("status") == "open"),
            "bucket_a": len(bucket_a),
            "bucket_b": len(bucket_b),
            "bucket_c": len(bucket_c),
            "skipped_in_flight": len(skipped_in_flight),
            "skipped_manual": len(skipped_manual),
        },
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
