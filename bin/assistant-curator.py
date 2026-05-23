#!/usr/bin/env python3
"""
assistant-curator — lessons-learned memory that grows.

A lesson is a rule the Assistant learned the hard way (typically by Mukul
correcting it). The Curator owns the lifecycle: write, list, consolidate,
archive. Only the Assistant's own lessons are subject to auto-archive;
user-pinned lessons are off-limits. Max destructive action is archive
(never delete).

Design adapted from the Hermes Agent Curator (Nous Research) — agent-curated
memory with periodic nudges and a stale-after-days lifecycle.

Subcommands:
  write   --trigger T --rule R --why W [--scope S] [--from-conv ID] [--pin]
  list    [--scope S] [--state active|stale|archived]
  show    <id>
  index   regenerate ~/.claude/lessons/index.md
  touch   <id>                       bump use_count, mark last_used
  archive <id> --reason ...
  unarchive <id>
  consolidate                        merge similar lessons (LLM-free dedup)
  stale-sweep                        mark unused-30d as stale
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ["HOME"])
LESSONS_DIR = HOME / ".claude/lessons"
ACTIVE_DIR = LESSONS_DIR / "active"
STALE_DIR = LESSONS_DIR / "stale"
ARCHIVE_DIR = LESSONS_DIR / "archive"
INDEX_PATH = LESSONS_DIR / "index.md"
USAGE_PATH = LESSONS_DIR / ".usage.json"

STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90
DEFAULT_SCOPE = "global"
ALLOWED_SCOPES = {
    "global", "executor", "dispatch", "classification", "dashboard",
    "todo", "ffp", "scout", "memory", "voice", "security",
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def slugify(text, n=40):
    text = re.sub(r"[^a-zA-Z0-9 ]+", "", text.lower()).strip()
    return re.sub(r"\s+", "-", text)[:n] or "lesson"


def ensure_dirs():
    for d in (ACTIVE_DIR, STALE_DIR, ARCHIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_usage():
    if USAGE_PATH.exists():
        return json.loads(USAGE_PATH.read_text())
    return {}


def save_usage(usage):
    USAGE_PATH.write_text(json.dumps(usage, indent=2, sort_keys=True))


def all_lesson_paths():
    return list(ACTIVE_DIR.glob("*.json")) + list(STALE_DIR.glob("*.json"))


def find_path(lesson_id):
    for d in (ACTIVE_DIR, STALE_DIR):
        p = d / f"{lesson_id}.json"
        if p.exists():
            return p
    for date_dir in ARCHIVE_DIR.glob("*"):
        p = date_dir / f"{lesson_id}.json"
        if p.exists():
            return p
    return None


def load(lesson_id):
    p = find_path(lesson_id)
    if not p:
        return None
    return json.loads(p.read_text()), p


def signature(trigger):
    """Fingerprint used to detect duplicate triggers."""
    norm = re.sub(r"\s+", " ", trigger.lower().strip())
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def cmd_write(args):
    ensure_dirs()
    ts = utc_now()
    sig = signature(args.trigger)
    # If a matching lesson already exists, bump use_count instead of duplicating.
    for p in all_lesson_paths():
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if data.get("signature") == sig:
            data["use_count"] = data.get("use_count", 0) + 1
            data["last_used_at"] = iso(ts)
            # Append the new source to the sources[] list if it has a conv id.
            if args.from_conv and not any(
                s.get("conversation_id") == args.from_conv
                for s in data.get("sources", [])
            ):
                data.setdefault("sources", []).append({
                    "conversation_id": args.from_conv,
                    "user_correction": args.why,
                    "ts": iso(ts),
                })
            p.write_text(json.dumps(data, indent=2))
            print(f"merged into existing lesson {data['id']} (use_count={data['use_count']})")
            return

    lesson_id = f"lesson-{int(ts.timestamp())}-{slugify(args.trigger)}"
    scope = args.scope or DEFAULT_SCOPE
    if scope not in ALLOWED_SCOPES:
        print(f"unknown scope: {scope} (allowed: {sorted(ALLOWED_SCOPES)})", file=sys.stderr)
        sys.exit(2)
    data = {
        "id": lesson_id,
        "signature": sig,
        "created_at": iso(ts),
        "created_by": "assistant",
        "trigger": args.trigger,
        "rule": args.rule,
        "why": args.why,
        "scope": scope,
        "pinned": bool(args.pin),
        "state": "active",
        "use_count": 1,
        "last_used_at": iso(ts),
        "violation_count": 0,
        "sources": [{
            "conversation_id": args.from_conv or "",
            "user_correction": args.why,
            "ts": iso(ts),
        }],
    }
    (ACTIVE_DIR / f"{lesson_id}.json").write_text(json.dumps(data, indent=2))
    print(f"wrote {lesson_id}")
    cmd_index(None)


def cmd_list(args):
    ensure_dirs()
    rows = []
    states = (args.state,) if args.state else ("active", "stale")
    for state in states:
        d = ACTIVE_DIR if state == "active" else STALE_DIR if state == "stale" else None
        if not d:
            continue
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            if args.scope and data.get("scope") != args.scope:
                continue
            rows.append(data)
    rows.sort(key=lambda r: (-r.get("pinned", False), -r.get("use_count", 0), r.get("created_at", "")))
    for r in rows:
        pin = "📌 " if r.get("pinned") else "   "
        print(f"{pin}{r['id'][:60]:60s} [{r.get('scope'):>14}] use={r.get('use_count', 0):>3}  {r.get('trigger', '')[:80]}")


def cmd_show(args):
    res = load(args.id)
    if not res:
        print(f"not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    data, path = res
    print(f"# {path.relative_to(HOME)}")
    print(json.dumps(data, indent=2))


def cmd_touch(args):
    res = load(args.id)
    if not res:
        print(f"not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    data, path = res
    data["use_count"] = data.get("use_count", 0) + 1
    data["last_used_at"] = iso(utc_now())
    path.write_text(json.dumps(data, indent=2))
    print(f"touched {args.id} (use_count={data['use_count']})")


def cmd_archive(args):
    res = load(args.id)
    if not res:
        print(f"not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    data, path = res
    if data.get("pinned"):
        print(f"refusing to archive pinned lesson {args.id}", file=sys.stderr)
        sys.exit(2)
    if data.get("created_by") != "assistant":
        print(f"refusing to archive user-authored lesson {args.id}", file=sys.stderr)
        sys.exit(3)
    now = utc_now()
    date_dir = ARCHIVE_DIR / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    data["state"] = "archived"
    data["archived_at"] = iso(now)
    data["archived_reason"] = args.reason or "manual"
    dest = date_dir / path.name
    dest.write_text(json.dumps(data, indent=2))
    path.unlink()
    print(f"archived {args.id} → {dest}")


def cmd_unarchive(args):
    p = None
    for date_dir in ARCHIVE_DIR.glob("*"):
        cand = date_dir / f"{args.id}.json"
        if cand.exists():
            p = cand
            break
    if not p:
        print(f"not in archive: {args.id}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(p.read_text())
    data["state"] = "active"
    data.pop("archived_at", None)
    data.pop("archived_reason", None)
    (ACTIVE_DIR / p.name).write_text(json.dumps(data, indent=2))
    p.unlink()
    print(f"unarchived {args.id}")


def cmd_stale_sweep(args):
    """Move active lessons unused for STALE_AFTER_DAYS into stale/."""
    ensure_dirs()
    now = utc_now()
    moved = 0
    for p in ACTIVE_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if data.get("pinned"):
            continue
        last = parse_iso(data.get("last_used_at") or data.get("created_at"))
        if last and (now - last) > timedelta(days=STALE_AFTER_DAYS):
            data["state"] = "stale"
            (STALE_DIR / p.name).write_text(json.dumps(data, indent=2))
            p.unlink()
            moved += 1
    print(f"stale-sweep: moved {moved}")

    # archive: stale lessons untouched for ARCHIVE_AFTER_DAYS
    archived = 0
    for p in STALE_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if data.get("pinned"):
            continue
        if data.get("created_by") != "assistant":
            continue
        last = parse_iso(data.get("last_used_at") or data.get("created_at"))
        if last and (now - last) > timedelta(days=ARCHIVE_AFTER_DAYS):
            date_dir = ARCHIVE_DIR / now.strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)
            data["state"] = "archived"
            data["archived_at"] = iso(now)
            data["archived_reason"] = f"unused for {ARCHIVE_AFTER_DAYS}d"
            (date_dir / p.name).write_text(json.dumps(data, indent=2))
            p.unlink()
            archived += 1
    print(f"archive-sweep: moved {archived}")


def cmd_consolidate(args):
    """Merge lessons with identical signatures. LLM-free."""
    ensure_dirs()
    by_sig = {}
    for p in all_lesson_paths():
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        by_sig.setdefault(data.get("signature"), []).append((p, data))
    merged = 0
    for sig, group in by_sig.items():
        if len(group) <= 1:
            continue
        # Keep the oldest, merge use_counts and sources into it.
        group.sort(key=lambda x: x[1].get("created_at", ""))
        keep_p, keep = group[0]
        for p, data in group[1:]:
            keep["use_count"] = keep.get("use_count", 0) + data.get("use_count", 0)
            for src in data.get("sources", []):
                if not any(s.get("conversation_id") == src.get("conversation_id")
                           for s in keep.get("sources", [])):
                    keep.setdefault("sources", []).append(src)
            p.unlink()
            merged += 1
        keep_p.write_text(json.dumps(keep, indent=2))
    print(f"consolidate: merged {merged} duplicate lessons")


def cmd_index(_args):
    ensure_dirs()
    rows = []
    for p in sorted(ACTIVE_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        rows.append(data)
    rows.sort(key=lambda r: (-r.get("pinned", False), -r.get("use_count", 0)))

    lines = [
        "# Lessons learned — Assistant memory",
        "",
        "Auto-generated by `assistant-curator.py index`. The Assistant reads this on session boot.",
        "",
        f"Active lessons: **{len(rows)}**. Stale: **{len(list(STALE_DIR.glob('*.json')))}**. "
        f"Last regenerated: {iso(utc_now())}.",
        "",
        "## Rules",
        "",
        "Each rule below was learned from a Mukul correction or a self-observed failure.",
        "Treat them as constraints on future behavior. If you must override one, say so out loud.",
        "",
    ]
    by_scope = {}
    for r in rows:
        by_scope.setdefault(r.get("scope", "global"), []).append(r)
    for scope in sorted(by_scope):
        lines.append(f"### {scope}")
        lines.append("")
        for r in by_scope[scope]:
            pin = " 📌" if r.get("pinned") else ""
            use = r.get("use_count", 0)
            lines.append(
                f"- **{r['trigger']}**{pin}\n"
                f"  - Rule: {r['rule']}\n"
                f"  - Why: {r['why']}\n"
                f"  - ID `{r['id']}` · used {use}×"
            )
        lines.append("")
    INDEX_PATH.write_text("\n".join(lines))
    print(f"index → {INDEX_PATH}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write")
    w.add_argument("--trigger", required=True)
    w.add_argument("--rule", required=True)
    w.add_argument("--why", required=True)
    w.add_argument("--scope")
    w.add_argument("--from-conv")
    w.add_argument("--pin", action="store_true")
    w.set_defaults(func=cmd_write)

    l = sub.add_parser("list")
    l.add_argument("--scope")
    l.add_argument("--state", choices=["active", "stale", "archived"])
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    t = sub.add_parser("touch")
    t.add_argument("id")
    t.set_defaults(func=cmd_touch)

    a = sub.add_parser("archive")
    a.add_argument("id")
    a.add_argument("--reason")
    a.set_defaults(func=cmd_archive)

    u = sub.add_parser("unarchive")
    u.add_argument("id")
    u.set_defaults(func=cmd_unarchive)

    sw = sub.add_parser("stale-sweep")
    sw.set_defaults(func=cmd_stale_sweep)

    c = sub.add_parser("consolidate")
    c.set_defaults(func=cmd_consolidate)

    i = sub.add_parser("index")
    i.set_defaults(func=cmd_index)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
