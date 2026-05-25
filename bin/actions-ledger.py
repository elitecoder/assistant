#!/usr/bin/env python3
"""actions-ledger — append-only ledger of every Assistant action.

Why: assistant-state.json is overwritten every pulse. When something goes
wrong, we have no way to look back at "what did the Assistant do during
pulses 100-200 that caused this?" — the evidence is gone.

This ledger is append-only JSONL. One line per action. Never overwritten.

Format:
    {"ts": "<UTC ISO>", "epoch": <int>, "pulse_idx": <int>,
     "key": "...", "kind": "...",
     "ws_ref": "workspace:N" | null,
     "td": "td-NNN" | null,
     "evidence": "...",
     "outcome": "verified|failed|skipped",
     "verdict": {applied_lessons, reasoning}     # optional, observer-emitted
    }

Path: ~/.assistant/actions-ledger.jsonl

Use:
    actions-ledger.py append --pulse-idx 348 --key assistant:dispatch:td-066 \
        --kind dispatch --ws-ref workspace:93 --td td-066 \
        --evidence "post-spawn validated SUBMITTED=1" --outcome verified

    actions-ledger.py tail [--n 20] [--ws workspace:NN]
    actions-ledger.py grep <pattern>
    actions-ledger.py rotate          # if file > 50MB, gzip and start new
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path(os.environ["HOME"]) / ".assistant/actions-ledger.jsonl"
ROTATE_BYTES = 50 * 1024 * 1024  # 50MB


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cmd_append(args) -> int:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": now_iso(),
        "epoch": int(time.time()),
        "pulse_idx": args.pulse_idx,
        "key": args.key,
        "kind": args.kind,
        "ws_ref": args.ws_ref,
        "td": args.td,
        "evidence": args.evidence,
        "outcome": args.outcome,
    }
    if args.verdict:
        try:
            entry["verdict"] = json.loads(args.verdict)
        except Exception:
            entry["verdict"] = args.verdict
    line = json.dumps(entry, ensure_ascii=False)
    with open(LEDGER_PATH, "a") as f:
        f.write(line + "\n")
    print(f"appended: {entry['key']}")
    return 0


def cmd_tail(args) -> int:
    if not LEDGER_PATH.exists():
        print("(ledger empty)")
        return 0
    n = args.n
    ws_filter = args.ws
    td_filter = args.td
    with open(LEDGER_PATH) as f:
        lines = f.readlines()
    if ws_filter:
        lines = [l for l in lines if ws_filter in l]
    if td_filter:
        lines = [l for l in lines if td_filter in l]
    for line in lines[-n:]:
        try:
            d = json.loads(line)
            ws = d.get("ws_ref") or "-"
            td = d.get("td") or "-"
            print(f"{d.get('ts','')}  pulse={d.get('pulse_idx',0):>4}  "
                  f"{d.get('outcome','?'):<8}  {ws:<14}  {td:<8}  "
                  f"{d.get('key','')}")
        except Exception:
            print(line, end="")
    return 0


def cmd_grep(args) -> int:
    if not LEDGER_PATH.exists():
        return 0
    import re
    pat = re.compile(args.pattern)
    with open(LEDGER_PATH) as f:
        for line in f:
            if pat.search(line):
                print(line, end="")
    return 0


def cmd_rotate(args) -> int:
    if not LEDGER_PATH.exists():
        print("(no ledger)")
        return 0
    size = LEDGER_PATH.stat().st_size
    if size < ROTATE_BYTES:
        print(f"size {size} < threshold {ROTATE_BYTES}; no rotation")
        return 0
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archived = LEDGER_PATH.with_name(f"actions-ledger.{ts}.jsonl.gz")
    with open(LEDGER_PATH, "rb") as src, gzip.open(archived, "wb") as dst:
        shutil.copyfileobj(src, dst)
    LEDGER_PATH.unlink()
    LEDGER_PATH.touch()
    print(f"rotated → {archived}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_append = sub.add_parser("append", help="append one action entry")
    p_append.add_argument("--pulse-idx", type=int, required=True)
    p_append.add_argument("--key", required=True)
    p_append.add_argument("--kind", required=True)
    p_append.add_argument("--ws-ref", default=None)
    p_append.add_argument("--td", default=None)
    p_append.add_argument("--evidence", default="")
    p_append.add_argument("--outcome", choices=["verified", "failed", "skipped", "rejected"], default="verified")
    p_append.add_argument("--verdict", default=None, help="JSON-encoded observer verdict (optional)")
    p_append.set_defaults(func=cmd_append)

    p_tail = sub.add_parser("tail", help="show last N entries")
    p_tail.add_argument("--n", type=int, default=30)
    p_tail.add_argument("--ws", default=None)
    p_tail.add_argument("--td", default=None)
    p_tail.set_defaults(func=cmd_tail)

    p_grep = sub.add_parser("grep", help="grep ledger for pattern")
    p_grep.add_argument("pattern")
    p_grep.set_defaults(func=cmd_grep)

    p_rotate = sub.add_parser("rotate", help="gzip current ledger if > 50MB")
    p_rotate.set_defaults(func=cmd_rotate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
