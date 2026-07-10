#!/usr/bin/env python3
"""plan-next-actions.py — the deterministic goals planner (Keel M4).

Thin CLI over src/assistant/goals.py, which owns all the logic (the pulse
imports the SAME module for its step-1.7 pass, so the scheduled path and this
on-demand path can never drift — the exact build-morning-brief.py ↔ brief.py
relationship).

Every pulse the planner:
  1. STAMPS each active goal's lastProgressAt mechanically (max over the actions
     ledger, merged PRs, resolved decisions, completed TODOs whose refs match
     the goal's links) — no prose, no LLM;
  2. in goal RANK order, stages the NEXT playbook step for any stalled goal that
     has leftover ACTIVE_WS_CAP headroom: an unattended step as an autoDispatch
     TODO (source="goal:<id>:<step-hash>", exact-source deduped → idempotent) —
     but ONLY when planner.autoDispatch is enabled; the SAFE DEFAULT stages a
     brief decision instead. Gated steps are always brief decisions.

Guards: `_paused:true` in goals.json → no-op (ledgered). A stale world.json →
no staging (ledgered). Saturated caps → ledgered skip. R3: ZERO new LLM spend —
nothing here or in goals.py reaches a `claude` call path.

    plan-next-actions.py                run one planner pass now
    plan-next-actions.py --stamp-only   only stamp progress, don't plan
    plan-next-actions.py --now EPOCH    run as-of a fixed instant (tests/replay)
    plan-next-actions.py --print        dump the full summary JSON to stdout

Pure stdlib.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--now", type=float, default=None,
                    help="run as-of this epoch (default: time.time())")
    ap.add_argument("--stamp-only", action="store_true",
                    help="only stamp mechanical progress; do not plan/stage")
    ap.add_argument("--print", dest="print_json", action="store_true",
                    help="dump the full summary JSON to stdout")
    args = ap.parse_args(argv)

    src_dir = str(REPO / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from assistant import goals  # noqa: PLC0415

    now = args.now if args.now is not None else time.time()
    if args.stamp_only:
        summary = {"stamp": goals.stamp_progress(now=now), "plan": None}
    else:
        stamp = goals.stamp_progress(now=now)
        summary = {"stamp": stamp, "plan": goals.plan_pass(now=now)}

    if args.print_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    plan = summary.get("plan") or {}
    print(json.dumps({
        "stamped": summary["stamp"].get("n", 0),
        "staged_todos": len(plan.get("staged_todos") or []),
        "staged_decisions": len(plan.get("staged_decisions") or []),
        "stalls": plan.get("stalls", 0),
        "paused": plan.get("paused", False),
        "stale_world": plan.get("stale_world", False),
    }, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
