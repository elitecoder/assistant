#!/usr/bin/env python3
"""link-msg — append a row to threads.jsonl tying a TG message to a ledger entry.

Used when Claude sends a message that wasn't a direct broadcast (e.g. an
ad-hoc summary) and wants future replies to it to be resolvable.

Usage:
  link-msg.py --tg-msg <id> --chat <id> --kind <action|urgent|reply|info>
              [--ledger-key <key>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402


def main(argv: list[str] | None = None, clock=None,
         paths: comms_lib.Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description="link a TG message to a ledger entry")
    ap.add_argument("--tg-msg", type=int, required=True, dest="tg_msg")
    ap.add_argument("--chat", type=int, required=True)
    ap.add_argument("--kind", required=True,
                    choices=["action", "urgent", "reply", "info"])
    ap.add_argument("--ledger-key", default=None, dest="ledger_key")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    comms_lib.append_thread(paths, args.ledger_key, args.tg_msg, args.chat,
                            args.kind, clock=clock)
    print("linked")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
