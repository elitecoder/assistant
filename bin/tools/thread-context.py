#!/usr/bin/env python3
"""thread-context — recent Slack conversation thread for one channel.

Thin wrapper over `conversation.py window --channel <channel>`, the sanctioned
reader for the durable chat log (~/.assistant/comms/conversation.jsonl). We
don't re-implement the windowing — conversation.py owns the turn/age bounds — we
just shell out and hand back its raw stdout under a single key.

Returns JSON to stdout:
  {"thread": "<raw JSON array text from conversation.py window>"}

On failure, "thread" is "" and an "error" key explains why.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
CONVERSATION = REPO / "bin" / "conversation.py"


def thread_context(channel: str) -> dict[str, Any]:
    if not CONVERSATION.exists():
        return {"thread": "", "error": f"conversation.py not found at {CONVERSATION}"}
    try:
        p = subprocess.run(
            [sys.executable, str(CONVERSATION), "window", "--channel", str(channel)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"thread": "", "error": "conversation.py window timed out"}
    except Exception as e:  # noqa: BLE001
        return {"thread": "", "error": str(e)}
    if p.returncode != 0:
        return {"thread": "", "error": (p.stderr or "").strip()[-300:]}
    return {"thread": p.stdout.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recent Slack conversation thread for a channel. Returns "
                    "JSON {thread: '<raw conversation.py window output>'}.")
    ap.add_argument("--channel", required=True, help="Slack channel id (D…/C…)")
    args = ap.parse_args()
    print(json.dumps(thread_context(args.channel)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
