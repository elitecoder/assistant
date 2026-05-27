#!/usr/bin/env python3
"""pick-ws-batch.py — list workspaces and pick the LRU batch for re-classification.

Mechanical only. The Assistant decides what to DO with the batch (run Agent
fan-out, emit verdicts, etc.). This script just enumerates and ranks.

Output: JSON
  {
    "to_reclassify": [{"ref": "...", "title": "...", "cwd": "..."}, ...],
    "reuse_cached": ["workspace:N", ...],
    "total_ws": N
  }
"""
import json
import os
import subprocess
import sys

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
SUMM_DIR = os.path.expanduser("~/.assistant/observer-summaries")
BACK_OFF_PATH = os.path.expanduser("~/.assistant/back-off.json")
BATCH_SIZE = 5


def load_back_off_refs():
    if not os.path.exists(BACK_OFF_PATH):
        return {}
    try:
        d = json.load(open(BACK_OFF_PATH))
    except Exception:
        return {}
    return {w.get("ws_ref"): w.get("reason", "") for w in d.get("workspaces", []) if w.get("ws_ref")}


def main():
    back_off = load_back_off_refs()

    out = subprocess.check_output([CMUX, "list-workspaces", "--json"], text=True)
    data = json.loads(out)
    items = data if isinstance(data, list) else data.get("workspaces", [])
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

    ranked = []
    for ws in ws_list:
        sf = os.path.join(SUMM_DIR, ws["ref"].replace(":", "_") + ".json")
        ts = 0
        if os.path.exists(sf):
            try:
                ts = int(json.load(open(sf)).get("last_updated_ts", 0))
            except Exception:
                ts = 0
        ranked.append((ts, ws))
    ranked.sort(key=lambda x: x[0])  # oldest first

    to_reclassify = [ws for _, ws in ranked[:BATCH_SIZE]]
    reuse_cached = [ws["ref"] for _, ws in ranked[BATCH_SIZE:]]

    print(json.dumps({
        "to_reclassify": to_reclassify,
        "reuse_cached": reuse_cached,
        "backed_off": backed_off,
        "total_ws": len(ws_list) + len(backed_off),
    }, indent=2))


if __name__ == "__main__":
    main()
