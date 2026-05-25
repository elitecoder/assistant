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
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HOME"])
CACHE_DIR = HOME / ".assistant/observer-summaries"


def compute_state_hash(verdict: dict) -> str:
    """Hash of the fields that signal observable state.

    classification + summary_for_next_pulse + sorted proposed_action kinds.
    Stable across pulses when nothing meaningful has changed.
    """
    cls = str(verdict.get("classification", ""))
    summ = str(verdict.get("summary_for_next_pulse", ""))
    kinds = sorted(
        str((a or {}).get("kind", ""))
        for a in (verdict.get("proposed_actions") or [])
    )
    payload = json.dumps([cls, summ, kinds], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


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

    now = int(time.time())
    new_hash = compute_state_hash(verdict)

    # Read prior summary to carry forward state-unchanged tracking.
    p = CACHE_DIR / f"{args.ws_ref.replace(':', '_')}.json"
    prior_hash = None
    state_unchanged_since_ts = now  # default: brand new entry
    if p.exists():
        try:
            prior = json.loads(p.read_text())
            prior_hash = prior.get("state_hash")
            if prior_hash == new_hash and prior.get("state_unchanged_since_ts"):
                state_unchanged_since_ts = int(prior["state_unchanged_since_ts"])
        except Exception:
            pass

    out = {
        **verdict,
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "pr_refs": pr_refs,
        "last_updated_ts": now,
        "state_hash": new_hash,
        "state_unchanged_since_ts": state_unchanged_since_ts,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(p)
    stuck_for = now - state_unchanged_since_ts
    print(f"saved: {p} (state_hash={new_hash} stuck_for={stuck_for}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
