#!/usr/bin/env python3
"""back-off.py — manage the Assistant's back-off list.

Workspaces on the list are skipped entirely by `pick-ws-batch.py`: no
Observer call, no send, no awaiting card. Use this when the Assistant
keeps doing the wrong thing on a workspace and you need it to stop right
now, without waiting for prompt fixes to propagate.

Usage:
    bin/back-off.py add workspace:N "reason here"
    bin/back-off.py remove workspace:N
    bin/back-off.py list

State lives at ~/.assistant/back-off.json:

    {"workspaces": [{"ws_ref": "workspace:N", "reason": "...", "added_ts": ...}]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PATH = Path.home() / ".assistant/back-off.json"


def load() -> dict:
    if not PATH.exists():
        return {"workspaces": []}
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {"workspaces": []}


def save(d: dict) -> None:
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(PATH)


def cmd_add(args) -> int:
    if not args.ws_ref.startswith("workspace:"):
        print(f"ws_ref must look like 'workspace:N', got {args.ws_ref!r}", file=sys.stderr)
        return 2
    d = load()
    for w in d["workspaces"]:
        if w.get("ws_ref") == args.ws_ref:
            w["reason"] = args.reason
            w["added_ts"] = int(time.time())
            save(d)
            print(f"updated {args.ws_ref}: {args.reason}")
            return 0
    d["workspaces"].append({
        "ws_ref": args.ws_ref,
        "reason": args.reason,
        "added_ts": int(time.time()),
    })
    save(d)
    print(f"added {args.ws_ref}: {args.reason}")
    return 0


def cmd_remove(args) -> int:
    d = load()
    before = len(d["workspaces"])
    d["workspaces"] = [w for w in d["workspaces"] if w.get("ws_ref") != args.ws_ref]
    if len(d["workspaces"]) == before:
        print(f"{args.ws_ref} not on back-off list")
        return 1
    save(d)
    print(f"removed {args.ws_ref}")
    return 0


def cmd_list(args) -> int:
    d = load()
    if not d["workspaces"]:
        print("(back-off list is empty)")
        return 0
    for w in d["workspaces"]:
        ts = w.get("added_ts", 0)
        age = int(time.time()) - ts if ts else -1
        print(f"  {w.get('ws_ref'):16s}  added={age}s ago  reason={w.get('reason','')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("ws_ref")
    a.add_argument("reason", nargs="?", default="(no reason given)")
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("remove")
    r.add_argument("ws_ref")
    r.set_defaults(func=cmd_remove)
    l = sub.add_parser("list")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
