#!/usr/bin/env python3
"""lookup-thread — find what a Slack message refers to.

Two query modes:

  lookup-thread.py --msg-ts <ts>
    → JSON of the matching thread record (last write wins) plus the full ledger
      entry (if --include-ledger and the ledger_key resolves):
      {"thread": {...}, "ledger": {...} | null}
    → exit 0 if found, 1 if not

  lookup-thread.py --ledger-key <key>
    → JSON list of every thread record for that key (one per channel we sent it
      to). Useful when Claude wants to know "did I already alert Mukul about
      this entry?"
    → exit 0 always (empty array if no match)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402


def find_ledger_entry_by_key(paths: comms_lib.Paths, key: str) -> dict | None:
    if not paths.ledger.exists():
        return None
    last: dict | None = None
    with open(paths.ledger) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("key") == key:
                last = rec  # if dupe-keys exist, take the most recent.
    return last


def main(argv: list[str] | None = None,
         paths: comms_lib.Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description="resolve a Slack message or ledger key")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--msg-ts", default=None, dest="msg_ts")
    grp.add_argument("--ledger-key", default=None, dest="ledger_key")
    ap.add_argument("--include-ledger", action="store_true", dest="include_ledger",
                    help="(--msg-ts only) also include the resolved ledger entry")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()

    if args.msg_ts is not None:
        rec = comms_lib.lookup_thread_by_msg_ts(paths, args.msg_ts)
        if rec is None:
            print(json.dumps({"thread": None, "ledger": None}))
            return 1
        ledger = None
        if args.include_ledger and rec.get("ledger_key"):
            ledger = find_ledger_entry_by_key(paths, rec["ledger_key"])
        print(json.dumps({"thread": rec, "ledger": ledger}))
        return 0

    rows = comms_lib.lookup_thread_by_ledger_key(paths, args.ledger_key)
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
