#!/usr/bin/env python3
"""obsidian-write — write a structured markdown note into Mukul's Obsidian vault.

The vault (~/dev/obs-elitecoder) is a human-readable, searchable knowledge base
of completed work, lessons, decisions, working style, and project knowledge.
This tool is the single sanctioned writer: the lesson-confirm and work-receipt
hooks call it, the one-time seeding passes call it, and the warm session can
call it by hand.

A note is YAML frontmatter + a markdown body:

    ---
    title: Connections P8 parity — shipped 2026-06-06
    created: 2026-06-06
    category: work_history
    tags:
      - shipped
      - pr-merged
    project: firefly-platform
    pr: 11034
    ---

    <body>

Layout — `--category` picks the subfolder unless `--folder` overrides it:

    lesson        -> Assistant/Lessons
    working_style -> Assistant/Working Style
    decision      -> Assistant/Decisions
    project       -> Projects
    work_history  -> Work Log          (the hook passes Work Log/<YYYY-MM> via --folder)

Filenames are YYYY-MM-DD-<slug>.md. Existing files are never overwritten — a
-2, -3, … suffix is appended on collision so re-running a seed is safe. The
write is atomic (tmp + os.replace) so a crash never leaves a torn note.

Stdout on success: {"path": "...", "status": "written"}. Exit 0.
Stdout on error:   {"error": "...", "status": "error"}. Exit 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VAULT_PATH = Path.home() / "dev" / "obs-elitecoder"

# Category -> default subfolder. --folder overrides; --category is the stable
# routing knob the hooks and seeders use.
CATEGORY_FOLDERS = {
    "lesson": "Assistant/Lessons",
    "working_style": "Assistant/Working Style",
    "decision": "Assistant/Decisions",
    "project": "Projects",
    "work_history": "Work Log",
}
CATEGORIES = tuple(CATEGORY_FOLDERS.keys())

# A YAML scalar needs quoting when it could be misread as a number, bool, null,
# or contains YAML-significant punctuation. We quote conservatively.
_NEEDS_QUOTE = re.compile(r"^[\s]|[\s]$|[:#\[\]{}&*!|>'\"%@`,]|^(true|false|null|yes|no|~|-)$|^[\d.+-]")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slugify(title: str) -> str:
    """Lowercase, non-alphanumerics -> single dash, trimmed. Always non-empty."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s or "note")[:80]


def _yaml_scalar(value: Any) -> str:
    """Render a Python value as a one-line YAML scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    if text == "" or _NEEDS_QUOTE.search(text):
        # Double-quote and escape embedded double-quotes/backslashes.
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def build_frontmatter(title: str, created: str, category: str | None,
                      tags: list[str], extra: dict[str, Any]) -> str:
    """Compose the YAML frontmatter block (including the --- fences).

    Ordering is deterministic: title, created, category, tags, then any extra
    frontmatter keys (sorted) — so re-seeding produces byte-identical headers."""
    lines = ["---"]
    lines.append(f"title: {_yaml_scalar(title)}")
    lines.append(f"created: {_yaml_scalar(created)}")
    if category:
        lines.append(f"category: {_yaml_scalar(category)}")
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {_yaml_scalar(t)}" for t in tags)
    for key in sorted(extra):
        if key in ("title", "created", "category", "tags"):
            continue  # never let extra frontmatter shadow the managed keys
        lines.append(f"{key}: {_yaml_scalar(extra[key])}")
    lines.append("---")
    return "\n".join(lines)


def _unique_path(folder: Path, date: str, slug: str) -> Path:
    """First free YYYY-MM-DD-slug[.-N].md path in folder. Never overwrites."""
    base = f"{date}-{slug}"
    candidate = folder / f"{base}.md"
    n = 2
    while candidate.exists():
        candidate = folder / f"{base}-{n}.md"
        n += 1
    return candidate


def write_note(*, vault: Path, title: str, body: str,
               category: str | None = None, folder: str | None = None,
               tags: list[str] | None = None,
               frontmatter: dict[str, Any] | None = None,
               date: str | None = None) -> dict[str, Any]:
    """Write one note. Returns {"path", "status"}. Raises OSError on FS failure.

    `folder` wins over `category` for placement; `category` still lands in the
    frontmatter so search can filter on it. Graceful if the vault is missing —
    it (and the target subfolder) are created."""
    tags = tags or []
    frontmatter = frontmatter or {}
    date = date or today_iso()

    if folder is None:
        folder = CATEGORY_FOLDERS.get(category or "", "")
    target_dir = (vault / folder) if folder else vault
    target_dir.mkdir(parents=True, exist_ok=True)

    fm = build_frontmatter(title, date, category, tags, frontmatter)
    content = f"{fm}\n\n# {title}\n\n{body.rstrip()}\n"

    out_path = _unique_path(target_dir, date, slugify(title))
    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out_path)
    return {"path": str(out_path), "status": "written"}


def _seed_note(vault: Path, s: dict[str, Any]) -> str:
    """Write one seed dict as a note; return 'written'. Work-history seeds get
    foldered by month to match the receipt hook's layout."""
    folder = None
    if s["category"] == "work_history":
        date = s.get("frontmatter", {}).get("date", "") or today_iso()
        folder = f"Work Log/{date[:7]}"
    write_note(vault=vault, title=s["title"], body=s["body"],
               category=s["category"], folder=folder, tags=s.get("tags"),
               frontmatter=s.get("frontmatter", {}),
               date=s.get("frontmatter", {}).get("date"))
    return "written"


def run_seed(vault: Path, include_lessons: bool) -> dict[str, int]:
    """Write seed notes for every category. include_lessons adds the confirmed-
    lesson backfill. Idempotency is by obsidian-write's never-overwrite
    suffixing — re-running creates -2/-3 duplicates, so seed once."""
    import memory_seeds as seeds
    counts: dict[str, int] = {}
    if include_lessons:
        lessons = seeds.confirmed_lessons()
        for s in lessons:
            _seed_note(vault, s)
        counts["lesson"] = len(lessons)
    for category, items in seeds.all_seeds().items():
        for s in items:
            _seed_note(vault, s)
        counts[category] = len(items)
    return counts


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ap = argparse.ArgumentParser(
        description="Write a structured markdown note into the Obsidian vault.")
    ap.add_argument("--vault", default=str(VAULT_PATH),
                    help=f"vault root (default {VAULT_PATH})")
    ap.add_argument("--seed-all", action="store_true",
                    help="write seed notes for every category (incl. lessons)")
    ap.add_argument("--backfill-lessons", action="store_true",
                    help="write a note for each confirmed lesson only")
    ap.add_argument("--title", default=None, help="note title (also the H1)")
    ap.add_argument("--body", default="", help="markdown body")
    ap.add_argument("--category", default=None, choices=CATEGORIES,
                    help="category -> default subfolder + frontmatter tag")
    ap.add_argument("--folder", default=None,
                    help="explicit subfolder, overrides --category placement")
    ap.add_argument("--tags", nargs="*", default=None, help="space-separated tags")
    ap.add_argument("--frontmatter", default=None,
                    help="extra frontmatter as a JSON object")
    ap.add_argument("--date", default=None, help="created date (default: today UTC)")
    args = ap.parse_args(argv)

    vault = Path(args.vault).expanduser()

    if args.seed_all or args.backfill_lessons:
        try:
            if args.backfill_lessons and not args.seed_all:
                import memory_seeds as seeds
                lessons = seeds.confirmed_lessons()
                for s in lessons:
                    _seed_note(vault, s)
                counts = {"lesson": len(lessons)}
            else:
                counts = run_seed(vault, include_lessons=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": str(e), "status": "error"}))
            return 1
        print(json.dumps({"seeded": counts, "status": "written",
                          "total": sum(counts.values())}))
        return 0

    if not args.title:
        print(json.dumps({"error": "--title is required (or use --seed-all / "
                          "--backfill-lessons)", "status": "error"}))
        return 1

    extra: dict[str, Any] = {}
    if args.frontmatter:
        try:
            extra = json.loads(args.frontmatter)
            if not isinstance(extra, dict):
                raise ValueError("frontmatter must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            print(json.dumps({"error": f"bad --frontmatter: {e}", "status": "error"}))
            return 1

    try:
        result = write_note(
            vault=vault,
            title=args.title, body=args.body, category=args.category,
            folder=args.folder, tags=args.tags, frontmatter=extra,
            date=args.date,
        )
    except Exception as e:  # noqa: BLE001 — surface as JSON, never traceback
        print(json.dumps({"error": str(e), "status": "error"}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
