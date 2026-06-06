#!/usr/bin/env python3
"""obsidian-search — keyword/frontmatter search over the Obsidian vault.

Greps note filenames + body text for a query and returns the best matches with
a short context snippet. No embeddings — this is the fast, dependency-free
lookup the warm session uses to recall a past note ("did I already write up
Connections P6?"). For semantic recall, mem0-search is the companion tool.

    obsidian-search.py --vault ~/dev/obs-elitecoder --query "Connections P6" --limit 5
    obsidian-search.py --query shipped --field category --value work_history

Matching:
  - --query  : case-insensitive substring over filename + body. Optional once
               --field/--value narrows the set, but usually present.
  - --field/--value : restrict to notes whose YAML frontmatter has field==value
               (exact, case-insensitive). Works with or without --query.

Implementation: a single `grep -ril` locates candidate files fast; we then read
each candidate to pull the title, verify the frontmatter filter, and cut a
snippet. If grep is unavailable (or errors), we fall back to a pure-Python walk
+ re scan over *.md — same results, slower.

Returns JSON to stdout:
  {"results": [{"path": "...", "title": "...", "category": "...",
                "snippet": "...~150 chars around the match..."}]}
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

VAULT_PATH = Path.home() / "dev" / "obs-elitecoder"
SNIPPET_RADIUS = 75  # chars on each side of the match -> ~150-char window

_FM_FENCE = re.compile(r"^---\s*$")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a note into (flat frontmatter dict, body). Only top-level scalar
    keys are captured — enough for filtering on category/project/tags-as-text.
    Returns ({}, text) when there is no leading --- block."""
    lines = text.splitlines()
    if not lines or not _FM_FENCE.match(lines[0]):
        return {}, text
    fm: dict[str, str] = {}
    i = 1
    while i < len(lines) and not _FM_FENCE.match(lines[i]):
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key, raw = m.group(1), m.group(2).strip()
            fm[key] = raw.strip('"').strip("'")
        i += 1
    body = "\n".join(lines[i + 1:]) if i < len(lines) else ""
    return fm, body


def _title_of(fm: dict[str, str], body: str, path: Path) -> str:
    if fm.get("title"):
        return fm["title"]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _snippet(haystack: str, needle: str) -> str:
    """~150-char window centered on the first case-insensitive match, or the
    leading slice of the body when there's no needle (frontmatter-only search)."""
    flat = " ".join(haystack.split())
    if not needle:
        return flat[: SNIPPET_RADIUS * 2].strip()
    idx = flat.lower().find(needle.lower())
    if idx < 0:
        return flat[: SNIPPET_RADIUS * 2].strip()
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(flat), idx + len(needle) + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end].strip()}{suffix}"


def _candidate_files_grep(vault: Path, query: str) -> list[Path] | None:
    """Files containing `query` (case-insensitive) via grep. None signals grep
    is unavailable/failed so the caller falls back to Python."""
    grep = shutil.which("grep")
    if not grep:
        return None
    try:
        proc = subprocess.run(
            [grep, "-rilZ", "--include=*.md", "-e", query, str(vault)],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    # rc 0 = matches, 1 = no matches (both fine); >1 = real error -> fall back.
    if proc.returncode > 1:
        return None
    out = proc.stdout
    parts = out.split("\0") if "\0" in out else out.splitlines()
    return [Path(p) for p in parts if p.strip()]


def _all_md_files(vault: Path) -> list[Path]:
    return [p for p in vault.rglob("*.md") if p.is_file()]


def search(vault: Path, query: str | None, field: str | None,
           value: str | None, limit: int) -> dict[str, Any]:
    if not vault.exists():
        return {"results": []}

    # 1. Narrow to candidate files. With a query, grep does the heavy lifting;
    #    filename matches are unioned in so titles match even when the body
    #    doesn't. Without a query (pure frontmatter filter), scan all notes.
    if query:
        by_grep = _candidate_files_grep(vault, query)
        candidates = set(by_grep) if by_grep is not None else set(
            p for p in _all_md_files(vault) if query.lower() in p.read_text(
                encoding="utf-8", errors="ignore").lower())
        for p in _all_md_files(vault):
            if query.lower() in p.name.lower():
                candidates.add(p)
    else:
        candidates = set(_all_md_files(vault))

    # 2. Read each candidate, apply the frontmatter filter, build the result.
    results: list[dict[str, Any]] = []
    for path in sorted(candidates):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm, body = _parse_frontmatter(text)
        if field is not None:
            have = fm.get(field, "")
            if value is not None and have.lower() != value.lower():
                continue
            if value is None and not have:
                continue
        results.append({
            "path": str(path),
            "title": _title_of(fm, body, path),
            "category": fm.get("category", ""),
            "snippet": _snippet(body or text, query or ""),
        })

    results.sort(key=lambda r: r["path"])
    return {"results": results[:limit] if limit > 0 else results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Keyword/frontmatter search over the Obsidian vault.")
    ap.add_argument("--vault", default=str(VAULT_PATH),
                    help=f"vault root (default {VAULT_PATH})")
    ap.add_argument("--query", default=None, help="case-insensitive substring")
    ap.add_argument("--field", default=None,
                    help="frontmatter field to filter on (e.g. category)")
    ap.add_argument("--value", default=None,
                    help="required value for --field (omit to match field presence)")
    ap.add_argument("--limit", type=int, default=5, help="max results (default 5)")
    args = ap.parse_args(argv)

    if not args.query and not args.field:
        print(json.dumps({"error": "supply --query and/or --field", "results": []}))
        return 1

    try:
        result = search(Path(args.vault).expanduser(), args.query,
                        args.field, args.value, args.limit)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "results": []}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
