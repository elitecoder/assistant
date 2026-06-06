#!/usr/bin/env python3
"""mem0-add — add a memory to the Assistant's semantic store.

Backed by mem0_backend (real mem0ai when available, local JSONL fallback). A
memory is a piece of content + metadata; the metadata.category field
(lesson|working_style|project|work_history|decision) lets mem0-search filter.

    mem0-add.py --content "Mukul approved merging P8 before P6.2 when CI green" \
      --category decision --metadata '{"project":"connections","date":"2026-06-06"}'

Seeding (one-time, idempotent — safe to re-run):
    mem0-add.py --seed-lessons    # confirmed lessons only
    mem0-add.py --seed-all        # lessons + working_style + project +
                                  # work_history + decision

Returns {"memory_id": "...", "status": "added|exists", "provider": "..."} for a
single add, or {"seeded": {<category>: {"added": N, "exists": M}}, ...} for a
seed run. Exit 0 on success, 1 on a bad-argument error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mem0_backend as mb  # noqa: E402
import memory_seeds as seeds  # noqa: E402

CATEGORIES = ("lesson", "working_style", "project", "work_history", "decision")


def _add_seed(backend: mb.MemoryBackend, s: dict[str, Any]) -> str:
    """Add one seed dict; return its status (added|exists)."""
    metadata = {"category": s["category"], "source": "seed"}
    metadata.update(s.get("frontmatter", {}))
    res = backend.add(s["content"], metadata)
    return res.get("status", "added")


def _seed_list(backend: mb.MemoryBackend, items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"added": 0, "exists": 0}
    for s in items:
        status = _add_seed(backend, s)
        counts["added" if status == "added" else "exists"] += 1
    return counts


def seed_lessons(backend: mb.MemoryBackend) -> dict[str, Any]:
    return {"lesson": _seed_list(backend, seeds.confirmed_lessons())}


def seed_all(backend: mb.MemoryBackend) -> dict[str, Any]:
    out: dict[str, Any] = {"lesson": _seed_list(backend, seeds.confirmed_lessons())}
    for category, items in seeds.all_seeds().items():
        out[category] = _seed_list(backend, items)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add a memory to the mem0 store.")
    ap.add_argument("--content", default=None, help="memory text")
    ap.add_argument("--user-id", dest="user_id", default=mb.USER_ID)
    ap.add_argument("--category", default=None, choices=CATEGORIES,
                    help="category tag (stored in metadata.category)")
    ap.add_argument("--metadata", default=None, help="extra metadata as JSON object")
    ap.add_argument("--seed-lessons", action="store_true",
                    help="seed all confirmed lessons (idempotent)")
    ap.add_argument("--seed-all", action="store_true",
                    help="seed every category (idempotent)")
    args = ap.parse_args(argv)

    backend = mb.MemoryBackend()

    if args.seed_all or args.seed_lessons:
        result = seed_all(backend) if args.seed_all else seed_lessons(backend)
        total_added = sum(c.get("added", 0) for c in result.values())
        total_exists = sum(c.get("exists", 0) for c in result.values())
        print(json.dumps({"seeded": result, "provider": backend.provider,
                          "total_added": total_added, "total_exists": total_exists}))
        return 0

    if not args.content:
        print(json.dumps({"error": "--content is required (or use --seed-all / "
                          "--seed-lessons)", "status": "error"}))
        return 1

    metadata: dict[str, Any] = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            print(json.dumps({"error": f"bad --metadata: {e}", "status": "error"}))
            return 1
    if args.category:
        metadata["category"] = args.category

    res = backend.add(args.content, metadata, user_id=args.user_id)
    res.setdefault("provider", backend.provider)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
