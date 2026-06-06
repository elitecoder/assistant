#!/usr/bin/env python3
"""mem0-search — semantic search over the Assistant's memory store.

Backed by mem0_backend (real mem0ai when available, local JSONL fallback). The
warm session calls this before answering to recall past decisions, lessons,
working-style preferences, and project knowledge.

    mem0-search.py --query "What did Mukul decide about Connections P6?" --limit 5
    mem0-search.py --query "what projects is Mukul working on" --category project

Returns:
    {"results": [{"memory": "...", "score": 0.92, "metadata": {...}}, ...],
     "provider": "local-jsonl|mem0ai"}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mem0_backend as mb  # noqa: E402

CATEGORIES = ("lesson", "working_style", "project", "work_history", "decision")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Semantic search over the mem0 store.")
    ap.add_argument("--query", required=True, help="natural-language query")
    ap.add_argument("--user-id", dest="user_id", default=mb.USER_ID)
    ap.add_argument("--limit", type=int, default=5, help="max results (default 5)")
    ap.add_argument("--category", default=None, choices=CATEGORIES,
                    help="restrict to one category")
    args = ap.parse_args(argv)

    mb.ensure_venv()  # hop into .venv-mem0 for real semantic search if needed
    backend = mb.MemoryBackend()
    try:
        results = backend.search(args.query, limit=args.limit,
                                 user_id=args.user_id, category=args.category)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "results": []}))
        return 1

    print(json.dumps({"results": results, "provider": backend.provider},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
