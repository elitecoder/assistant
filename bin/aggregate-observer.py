#!/usr/bin/env python3
"""aggregate-observer.py — fold all observer-summaries/*.json into one report.

Mechanical only. The Assistant prompt reads the report and decides what to
execute. This script just accumulates the per-ws verdicts on disk and
returns the merged candidate_actions / draft_awaiting_cards / counts.

Output: writes JSON to stdout AND to /tmp/observer-report-<pid>.json.
"""
import glob
import json
import os
import sys

SUMM_DIR = os.path.expanduser("~/.assistant/observer-summaries")


def main():
    candidate_actions = []
    draft_cards = []
    counts = {}
    total = 0
    for f in glob.glob(os.path.join(SUMM_DIR, "*.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        cls = s.get("classification", "UNKNOWN")
        counts[cls] = counts.get(cls, 0) + 1
        total += 1
        for a in s.get("proposed_actions") or []:
            p = dict(a.get("params") or {})
            if "ws_ref" not in p:
                p["ws_ref"] = s.get("ws_ref")
            candidate_actions.append({
                **a, "params": p,
                "_source_ws": s.get("ws_ref"),
                "_classification": cls,
            })
        if s.get("draft_card"):
            draft_cards.append(s["draft_card"])

    report = {
        "_meta": {"total": total, "classification_counts": counts},
        "candidate_actions": candidate_actions,
        "draft_awaiting_cards": draft_cards,
    }
    out_path = f"/tmp/observer-report-{os.getpid()}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps({
        "report_path": out_path,
        "actions": len(candidate_actions),
        "cards": len(draft_cards),
        "total_ws": total,
    }))


if __name__ == "__main__":
    main()
