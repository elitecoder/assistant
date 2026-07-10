#!/usr/bin/env python3
"""pick-ws-batch.py — list workspaces and pick the LRU batch for re-classification.

Mechanical only. The Assistant decides what to DO with the batch (run Agent
fan-out, emit verdicts, etc.). This script just enumerates and ranks.

Ranking: workspaces with a WorldEvent (refs.ws_ref in ~/.assistant/events.jsonl)
NEWER than their last observer summary are promoted ahead of everything else —
event-priority beats LRU, so a needs_input signal is observed on the very next
pulse instead of waiting its turn through the 30-min LRU floor. Within the
promoted set, the longest-waiting event goes first. Everything else keeps the
LRU order (oldest summary first).

Output: JSON
  {
    "to_reclassify": [{"ref": "...", "title": "...", "cwd": "..."}, ...],
    "reuse_cached": ["workspace:N", ...],
    "total_ws": N
  }
Promoted entries carry "event_priority": true for the trace.
"""
import json
import os
import subprocess
import sys
from datetime import datetime

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
SUMM_DIR = os.path.expanduser("~/.assistant/observer-summaries")
BACK_OFF_PATH = os.path.expanduser("~/.assistant/back-off.json")
EVENTS_PATH = os.path.expanduser("~/.assistant/events.jsonl")
BATCH_SIZE = 5
# Tail window for the event scan. Events are ~300B rows; 200KB covers days of
# fleet signal — bounded so a years-old log can't slow the 5-min pulse.
EVENTS_TAIL_BYTES = 200_000


def load_back_off_refs():
    if not os.path.exists(BACK_OFF_PATH):
        return {}
    try:
        d = json.load(open(BACK_OFF_PATH))
    except Exception:
        return {}
    return {w.get("ws_ref"): w.get("reason", "") for w in d.get("workspaces", []) if w.get("ws_ref")}


def prune_back_off(live_refs):
    """Drop back-off entries whose workspace is no longer in cmux.

    Returns the count pruned. No-op (and silent) if the file is missing or
    unparseable, or if every entry is still live.
    """
    if not os.path.exists(BACK_OFF_PATH):
        return 0
    try:
        d = json.load(open(BACK_OFF_PATH))
    except Exception:
        return 0
    workspaces = d.get("workspaces") or []
    kept = [w for w in workspaces if w.get("ws_ref") in live_refs]
    pruned = len(workspaces) - len(kept)
    if pruned == 0:
        return 0
    d["workspaces"] = kept
    tmp = BACK_OFF_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, BACK_OFF_PATH)
    return pruned


def load_event_promotions():
    """Map ws_ref → latest WorldEvent epoch from the tail of events.jsonl.

    Read-only and fully fenced: a missing/corrupt events log just means no
    promotions (pure LRU), never a failed batch pick.
    """
    if not os.path.exists(EVENTS_PATH):
        return {}
    try:
        with open(EVENTS_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - EVENTS_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return {}
    out = {}
    for line in tail.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        ws_ref = (d.get("refs") or {}).get("ws_ref")
        if not ws_ref:
            continue
        epoch = d.get("epoch")
        if not isinstance(epoch, (int, float)):
            try:
                epoch = datetime.fromisoformat(
                    str(d.get("ts")).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
        if epoch > out.get(ws_ref, 0):
            out[ws_ref] = epoch
    return out


def main():
    out = subprocess.check_output([CMUX, "list-workspaces", "--json"], text=True)
    data = json.loads(out)
    items = data if isinstance(data, list) else data.get("workspaces", [])
    live_refs = {w["ref"] for w in items if w.get("ref")}

    prune_back_off(live_refs)
    back_off = load_back_off_refs()

    ws_list = []
    backed_off = []
    for w in items:
        if not w.get("ref"):
            continue
        if w["ref"] in back_off:
            backed_off.append({"ref": w["ref"], "title": (w.get("title") or "").strip(), "reason": back_off[w["ref"]]})
            continue
        ws_list.append({
            "ref": w["ref"],
            "title": (w.get("title") or "").strip(),
            "cwd": w.get("current_directory") or "",
        })

    # Event-priority beats LRU: a ws whose latest WorldEvent is newer than its
    # last summary has something unobserved — it jumps the queue (Keel M1).
    promotions = load_event_promotions()
    promoted = []  # (event_epoch, ws) — oldest unobserved event first
    lru = []       # (summary_ts, ws) — oldest summary first, as before
    for ws in ws_list:
        sf = os.path.join(SUMM_DIR, ws["ref"].replace(":", "_") + ".json")
        ts = 0
        if os.path.exists(sf):
            try:
                ts = int(json.load(open(sf)).get("last_updated_ts", 0))
            except Exception:
                ts = 0
        ev_epoch = promotions.get(ws["ref"], 0)
        if ev_epoch > ts:
            ws["event_priority"] = True
            promoted.append((ev_epoch, ws))
        else:
            lru.append((ts, ws))
    promoted.sort(key=lambda x: x[0])
    lru.sort(key=lambda x: x[0])
    ordered = [ws for _, ws in promoted] + [ws for _, ws in lru]

    to_reclassify = ordered[:BATCH_SIZE]
    reuse_cached = [ws["ref"] for ws in ordered[BATCH_SIZE:]]

    print(json.dumps({
        "to_reclassify": to_reclassify,
        "reuse_cached": reuse_cached,
        "backed_off": backed_off,
        "total_ws": len(ws_list) + len(backed_off),
    }, indent=2))


if __name__ == "__main__":
    main()
