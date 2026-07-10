#!/usr/bin/env python3
"""build-morning-brief.py — on-demand morning-brief (re)build (Keel M3).

Thin CLI over src/assistant/brief.py, which owns all the logic (the pulse
imports the same module for its step-1.6 build, so the scheduled path and
this on-demand path can never drift). The brief is a PURE DERIVATION over
the decision queue, actions ledger, digest files, metering data and
world.json — deleting ~/.assistant/brief/brief-<date>.json and re-running
this reproduces it byte-for-byte for the same instant, which is exactly what
the delete-and-rebuild test does.

    build-morning-brief.py                 rebuild today's brief now
    build-morning-brief.py --print         also dump the brief JSON to stdout
    build-morning-brief.py --degrade       also run the unseen-brief
                                           degradation pass (>48h-unseen
                                           briefs TTL non-escalate decisions
                                           to digest + feed the miner)
    build-morning-brief.py --now EPOCH     build as-of a fixed instant
                                           (tests/replay)

The daily north-star metrics row appends at most once per date, so an
on-demand rebuild never double-books the metric. Pure stdlib, no LLM.
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
                    help="build as-of this epoch (default: time.time())")
    ap.add_argument("--print", dest="print_json", action="store_true",
                    help="dump the brief JSON to stdout")
    ap.add_argument("--degrade", action="store_true",
                    help="also run the unseen-brief degradation pass")
    args = ap.parse_args(argv)

    src_dir = str(REPO / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from assistant import brief  # noqa: PLC0415

    now = args.now if args.now is not None else time.time()
    doc = brief.build_brief(now=now)
    path = brief.write_brief(doc)
    metrics_row = brief.append_daily_metrics(doc, now=now)
    summary = {
        "path": str(path),
        "date": doc["date"],
        "open_decisions": doc["counts"]["open_decisions"],
        "handled_overnight": doc["counts"]["handled_overnight"],
        "digest_rows": doc["counts"]["digest_rows"],
        "metrics_row_appended": metrics_row is not None,
    }
    if args.degrade:
        summary["degrade"] = brief.degrade_unseen(now=now)
    if args.print_json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
