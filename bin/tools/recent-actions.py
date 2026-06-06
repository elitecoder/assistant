#!/usr/bin/env python3
"""recent-actions — last N actions from the Assistant action ledger.

Reads ~/.assistant/actions-ledger.jsonl, returns the most recent N entries
(optionally filtered to one workspace), newest last. Each entry is projected
down to the fields the warm session cares about: ts, kind, ws_ref, outcome,
evidence.

Returns JSON to stdout:
  {"actions": [{"ts": "...", "kind": "...", "ws_ref": "...",
                "outcome": "...", "evidence": "..."}]}

The ledger is append-only JSONL; malformed lines are skipped. We read the
whole file (it's small and rotated elsewhere) and slice in memory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HOME = Path.home()
LEDGER_PATH = HOME / ".assistant" / "actions-ledger.jsonl"

PROJECT_FIELDS = ("ts", "kind", "ws_ref", "outcome", "evidence")


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def recent_actions(n: int = 10, ws: str | None = None) -> dict[str, Any]:
    entries = _read_entries(LEDGER_PATH)
    if ws:
        entries = [e for e in entries if e.get("ws_ref") == ws]
    if n > 0:
        entries = entries[-n:]
    projected = [
        {k: e.get(k) for k in PROJECT_FIELDS}
        for e in entries
    ]
    return {"actions": projected}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Last N verified actions from the ledger (newest last). "
                    "Returns JSON {actions: [...]}.")
    ap.add_argument("--n", type=int, default=10,
                    help="how many recent actions to return (default 10)")
    ap.add_argument("--ws", default=None, help="filter to one workspace ref")
    args = ap.parse_args()
    print(json.dumps(recent_actions(args.n, args.ws)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
