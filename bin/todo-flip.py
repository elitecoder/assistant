#!/usr/bin/env python3
"""todo-flip.py — atomically edit ~/.claude/assistant-todo.json.

Mechanical only. The Assistant decides which TODO to flip and to what
status (and validates NAB / confidence rules first). This script just
does the edit.

Usage:
  todo-flip.py --id td-NNN --status done|deferred|in-progress|blocked|open \
               [--reason "one-line evidence quote"]
  todo-flip.py --id td-NNN --dispatched WS_REF --dispatched-at NOW
"""
import argparse
import datetime
import json
import os
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True)
    p.add_argument("--status", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--dispatched", default="")
    p.add_argument("--dispatched-at", default="")
    args = p.parse_args()

    path = os.path.expanduser("~/.claude/assistant-todo.json")
    d = json.load(open(path))
    now = datetime.datetime.now(datetime.UTC).isoformat()
    found = False
    for it in d["items"]:
        if it["id"] == args.id:
            found = True
            if args.status:
                it["status"] = args.status
                it["statusUpdatedAt"] = now
                if args.reason:
                    it["statusReason"] = args.reason
            if args.dispatched:
                it["dispatchedWs"] = args.dispatched
                it["dispatchedAt"] = args.dispatched_at or now
                it["statusUpdatedAt"] = now
            break
    if not found:
        print(f"id_not_found:{args.id}", file=sys.stderr)
        sys.exit(1)

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
