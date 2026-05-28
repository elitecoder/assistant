#!/usr/bin/env python3
"""assistant-curator — lessons live in ~/.claude/CLAUDE.md.

A lesson is a rule. Stored as a markdown block inside the `## Lessons`
section of `~/.claude/CLAUDE.md`. Every Claude Code session auto-loads
CLAUDE.md, so any agent can read these without extra wiring.

Block format:

    <!-- lesson: <slug>, scope: <scope>, added: <YYYY-MM-DD> -->
    **<one-line trigger>**

    <rule body — one paragraph>

Subcommands:
    write    --trigger T --rule R [--scope S] [--slug S]
    list     [--scope S]
    rm       <slug>
    trim     interactive triage (open in $EDITOR)

There's no `why`, no `pinned`, no `use_count`, no `state`. A rule is a rule;
remove it when it's stale, edit it when it's wrong. Source of truth is the
markdown — `git diff ~/.claude/CLAUDE.md` is the audit log.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ["HOME"])
CLAUDE_MD = HOME / ".claude/CLAUDE.md"
SECTION_HEADING = "## Lessons"

DEFAULT_SCOPE = "global"
ALLOWED_SCOPES = {
    "global", "classification", "dashboard",
    "ffp", "scout", "memory", "security",
}


def today():
    return datetime.now(timezone.utc).date().isoformat()


def slugify(text: str, n: int = 5) -> str:
    """Turn a trigger into a short kebab-case slug. ~5 words max."""
    text = re.sub(r"[^a-zA-Z0-9 ]+", " ", text.lower()).strip()
    words = [w for w in text.split() if w][:n]
    return "-".join(words) or "lesson"


def read_claude_md() -> str:
    if not CLAUDE_MD.exists():
        return ""
    return CLAUDE_MD.read_text()


def write_claude_md(text: str) -> None:
    tmp = CLAUDE_MD.with_suffix(".md.tmp")
    tmp.write_text(text)
    tmp.replace(CLAUDE_MD)


def find_section_bounds(text: str) -> tuple[int, int] | None:
    """Return (start, end) char offsets of the lessons section body, or None."""
    m = re.search(rf"^{re.escape(SECTION_HEADING)}\s*$", text, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    # End is either the next H2 or EOF.
    next_h2 = re.search(r"^## ", text[start:], re.MULTILINE)
    end = start + next_h2.start() if next_h2 else len(text)
    return (start, end)


def ensure_section(text: str) -> str:
    """Append an empty `## Lessons` section if missing."""
    if find_section_bounds(text) is not None:
        return text
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + (
        f"{SECTION_HEADING}\n\n"
        "Rules learned from past incidents. Each block is a rule. Edit / delete freely.\n"
        "Curator: `~/.claude/bin/assistant-curator.py write|trim|list`.\n\n"
    )


# Block format: HTML comment header, then a paragraph that starts with a
# **bolded trigger sentence** immediately followed by the rule body. The
# block ends at the next `<!-- lesson:` header or the end of the section.
#
# Triggers may contain literal `*` (e.g. globs like `*.ips`), so the trigger
# ends at the FIRST `**` that is followed by either `\s` or punctuation —
# i.e. closing-bold, not opening-bold. Cheapest correct rule: split the
# block on `**`. Pieces are [pre, trigger, rule_continuation, ...]. Joining
# any extras back into the rule keeps embedded inline-bold inside the rule
# working correctly.
LESSON_HEADER_RE = re.compile(
    r"<!--\s*lesson:\s*(?P<slug>[a-z0-9\-]+),"
    r"\s*scope:\s*(?P<scope>[a-z]+),"
    r"\s*added:\s*(?P<added>\d{4}-\d{2}-\d{2})\s*-->",
)


def iter_lessons(text: str):
    bounds = find_section_bounds(text)
    if bounds is None:
        return
    body = text[bounds[0]:bounds[1]]
    headers = list(LESSON_HEADER_RE.finditer(body))
    for i, h in enumerate(headers):
        next_start = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        block = body[h.start():next_start]
        # Strip the header line.
        try:
            after_header = block.split("-->\n", 1)[1]
        except IndexError:
            continue
        # Split on `**`. Expect at least 3 parts: ['', trigger, rule_rest...].
        parts = after_header.split("**")
        if len(parts) < 3 or parts[0].strip():
            continue
        trigger = parts[1].strip()
        # Re-join everything after the closing `**` of the trigger; wrap any
        # subsequent `**...**` pairs as inline bold by joining with `**`.
        rule = "**".join(parts[2:]).strip()
        yield {
            "slug": h.group("slug"),
            "scope": h.group("scope"),
            "added": h.group("added"),
            "trigger": trigger,
            "rule": rule,
            "body_offset": bounds[0] + h.start(),
            "body_end": bounds[0] + next_start,
        }


def cmd_write(args) -> int:
    trigger = args.trigger.strip()
    rule = args.rule.strip()
    if not trigger or not rule:
        print("ERROR: --trigger and --rule are required and non-empty.", file=sys.stderr)
        return 2
    scope = args.scope or DEFAULT_SCOPE
    if scope not in ALLOWED_SCOPES:
        print(
            f"ERROR: scope {scope!r} not in {sorted(ALLOWED_SCOPES)}",
            file=sys.stderr,
        )
        return 2
    slug = args.slug or slugify(f"{scope}-{trigger}" if scope != DEFAULT_SCOPE else trigger)

    text = read_claude_md()
    text = ensure_section(text)

    # Reject duplicate slug.
    for L in iter_lessons(text):
        if L["slug"] == slug:
            print(f"ERROR: lesson slug {slug!r} already exists.", file=sys.stderr)
            return 2

    block = (
        f"<!-- lesson: {slug}, scope: {scope}, added: {today()} -->\n"
        f"**{trigger}**\n\n"
        f"{rule}\n\n"
    )
    bounds = find_section_bounds(text)
    assert bounds is not None
    insert_at = bounds[1]
    new_text = text[:insert_at] + block + text[insert_at:]
    write_claude_md(new_text)
    print(f"wrote lesson: {slug}")
    return 0


def cmd_list(args) -> int:
    text = read_claude_md()
    lessons = list(iter_lessons(text))
    if args.scope:
        lessons = [L for L in lessons if L["scope"] == args.scope]
    if not lessons:
        print("(no lessons)")
        return 0
    for L in lessons:
        print(f"  [{L['scope']:8}] {L['slug']:50} {L['added']}")
        print(f"           {L['trigger'][:90]}")
    print(f"\n{len(lessons)} lesson(s)")
    return 0


def cmd_rm(args) -> int:
    text = read_claude_md()
    target = None
    for L in iter_lessons(text):
        if L["slug"] == args.slug:
            target = L
            break
    if target is None:
        print(f"ERROR: no lesson with slug {args.slug!r}", file=sys.stderr)
        return 2
    new_text = text[:target["body_offset"]] + text[target["body_end"]:]
    # Collapse extra blank lines created by the deletion.
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    write_claude_md(new_text)
    print(f"removed lesson: {args.slug}")
    return 0


def cmd_trim(args) -> int:
    """Open ~/.claude/CLAUDE.md in $EDITOR. The user is the trim mechanism."""
    editor = os.environ.get("EDITOR", "vi")
    print(f"opening {CLAUDE_MD} in {editor}...")
    print(f"(go to '{SECTION_HEADING}' section. Delete blocks you don't want anymore.)")
    os.execvp(editor, [editor, str(CLAUDE_MD)])


def main() -> int:
    ap = argparse.ArgumentParser(prog="assistant-curator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_write = sub.add_parser("write", help="add a new lesson")
    p_write.add_argument("--trigger", required=True)
    p_write.add_argument("--rule", required=True)
    p_write.add_argument("--scope", default=DEFAULT_SCOPE,
                         help=f"one of: {sorted(ALLOWED_SCOPES)} "
                              f"(dispatcher-only scopes {sorted(DISPATCHER_ONLY_SCOPES)} go in the Assistant prompt instead)")
    p_write.add_argument("--slug", help="override the auto-generated slug")
    p_write.set_defaults(func=cmd_write)

    p_list = sub.add_parser("list", help="list lessons")
    p_list.add_argument("--scope")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("rm", help="remove a lesson by slug")
    p_rm.add_argument("slug")
    p_rm.set_defaults(func=cmd_rm)

    p_trim = sub.add_parser("trim", help="open CLAUDE.md in $EDITOR for triage")
    p_trim.set_defaults(func=cmd_trim)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
