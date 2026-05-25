#!/usr/bin/env python3
"""save-ws-summary — atomic write of one workspace's verdict to disk.

Pure data persistence. The Assistant's main pulse calls this after each
per-ws Agent tool call returns its verdict, so subsequent pulses can reuse
the verdict (or skip the agent call entirely if no JSONL bytes have changed).

Usage:
    bin/save-ws-summary.py --ws-ref workspace:N \\
                           --title "..." \\
                           --cwd /Users/.../firefly-platform \\
                           --pr-refs '[10320, 10326]' \\
                           --json '{...verdict from agent...}'

The verdict JSON should match the per-ws agent's output schema:
    {classification, proposed_actions[], draft_card, summary_for_next_pulse, last_seen_ts}

This script merges in {title, cwd, pr_refs, last_updated_ts} and writes
atomically to ~/.assistant/observer-summaries/<ws_ref>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HOME"])
CACHE_DIR = HOME / ".assistant/observer-summaries"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ws-ref", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--pr-refs", default="[]", help="JSON array of PR numbers")
    ap.add_argument("--json", required=True, help="JSON verdict from per-ws agent")
    args = ap.parse_args()

    try:
        verdict = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"ERROR: --json failed to parse: {e}", file=sys.stderr)
        return 2
    if not isinstance(verdict, dict):
        print(f"ERROR: --json must be a JSON object, got {type(verdict).__name__}", file=sys.stderr)
        return 2

    try:
        pr_refs = json.loads(args.pr_refs)
    except Exception:
        pr_refs = []

    out = {
        **verdict,
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "pr_refs": pr_refs,
        "last_updated_ts": int(time.time()),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{args.ws_ref.replace(':', '_')}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(p)
    print(f"saved: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
