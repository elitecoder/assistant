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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ["HOME"])
SECTION_HEADING = "## Lessons"


def _project_blurb(name: str) -> str:
    """Descriptive text placed directly under the `## Lessons` heading of a
    fresh project rules file. Short — it names the project and the curator."""
    return (
        f"Project-scoped rules for {name}, managed by "
        "`assistant-curator.py write --target ...`. Each block is a rule that "
        "travels with this repo and applies to every session that reads a file "
        "matching this file's `paths:` glob.\n"
    )


def _project_preamble(name: str) -> str:
    """The path-scoped-rule frontmatter + H1 that must sit ABOVE the `## Lessons`
    section in a project rules file. Without the `paths:` frontmatter the file
    is never injected into session context, so a brand-new file gets this."""
    return (
        "---\n"
        "paths:\n"
        '  - "**/*"\n'
        "---\n\n"
        f"# {name} Lessons\n\n"
        "<!-- Rules below are managed by `assistant-curator.py write --target ...`. "
        "Keep the `## Lessons` heading — the curator uses it as the insert anchor. -->\n\n"
    )


# Lesson stores, picked by --target. Three flavors:
#
#   Session stores (no "repo" key) — loaded automatically by a running agent:
#     claude    → ~/.claude/CLAUDE.md — rules EVERY Claude Code session obeys.
#                 Personal behavior, never project-specific. Mirrored to the
#                 cross-machine memory repo.
#     assistant → a `## Lessons` section in the Observer batch prompt. The
#                 Observer loads that prompt every pulse, so a verdict-policy
#                 rule ("never send /cleanup to a session awaiting my review")
#                 takes effect with no extra wiring — and does NOT pollute
#                 CLAUDE.md, which every unrelated coding session loads.
#
#   Project stores (have a "repo" key) — a path-scoped `.claude/rules/*.md` file
#   that lives INSIDE the project repo, so the rule applies to all users, all
#   machines, and travels with the code. After a write the lesson is auto-
#   committed to that repo (best-effort, non-blocking). Project-scoped lessons
#   (archffp/firefly-platform/assistant-repo specifics) belong here, NOT in
#   CLAUDE.md where they'd load for every unrelated session.
ASSISTANT_REPO = Path(__file__).resolve().parent.parent
FFP_REPO = HOME / "dev/firefly-platform"
ARCHFFP_REPO = HOME / "dev/architect-ffp"
TARGETS = {
    "claude": {
        "path": HOME / ".claude/CLAUDE.md",
        "scopes": {"global", "classification", "dashboard",
                   "scout", "memory", "security"},
        "default_scope": "global",
    },
    "assistant": {
        "path": ASSISTANT_REPO / "prompts/observer-batch-prompt.md",
        # Sub-domains of the Observer's verdict judgment.
        "scopes": {"verdict", "merge", "cleanup", "stranded", "general"},
        "default_scope": "general",
    },
    "ffp": {
        "path": FFP_REPO / ".claude/rules/ffp-lessons.md",
        "scopes": {"squirrel", "ecs", "e2e", "ci", "jira", "ffp", "general"},
        "default_scope": "general",
        "repo": FFP_REPO,
        "blurb": _project_blurb("FFP / firefly-platform"),
        "preamble": _project_preamble("FFP / firefly-platform"),
    },
    "archffp": {
        "path": ARCHFFP_REPO / ".claude/rules/archffp-lessons.md",
        "scopes": {"pipeline", "cleanup", "ci", "eval", "archffp", "general"},
        "default_scope": "general",
        "repo": ARCHFFP_REPO,
        "blurb": _project_blurb("architect-ffp"),
        "preamble": _project_preamble("architect-ffp"),
    },
    "assistant-repo": {
        "path": ASSISTANT_REPO / ".claude/rules/assistant-lessons.md",
        "scopes": {"daemon", "pulse", "memory", "comms", "general"},
        "default_scope": "general",
        "repo": ASSISTANT_REPO,
        "blurb": _project_blurb("Assistant"),
        "preamble": _project_preamble("Assistant"),
    },
}
DEFAULT_TARGET = "claude"


def today():
    return datetime.now(timezone.utc).date().isoformat()


def slugify(text: str, n: int = 5) -> str:
    """Turn a trigger into a short kebab-case slug. ~5 words max."""
    text = re.sub(r"[^a-zA-Z0-9 ]+", " ", text.lower()).strip()
    words = [w for w in text.split() if w][:n]
    return "-".join(words) or "lesson"


def read_store(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text()


def write_store(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


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


def ensure_section(text: str, blurb: str | None = None,
                   preamble: str | None = None) -> str:
    """Append an empty `## Lessons` section if missing.

    `preamble` is content that must sit ABOVE the `## Lessons` heading when the
    store is brand-new — for project rules files that means the `paths:`
    frontmatter + H1, without which the file is never injected into session
    context. It is only used when `text` is empty (a fresh file); an existing
    file already has its own header and is left untouched above the section."""
    if find_section_bounds(text) is not None:
        return text
    blurb = blurb or (
        "Rules learned from past incidents. Each block is a rule. Edit / delete freely.\n"
        "Curator: `~/.claude/bin/assistant-curator.py write|trim|list`.\n"
    )
    if not text.strip() and preamble:
        return preamble + f"{SECTION_HEADING}\n\n{blurb}\n"
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + f"{SECTION_HEADING}\n\n{blurb}\n"


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
    r"\s*scope:\s*(?P<scope>[a-z0-9]+),"
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


# A lesson's scope can betray that it's filed in the wrong store. The clearest
# signal: a project-specific scope sitting in a session store (claude/assistant)
# that — with project routing now available — belongs in a project store. Map
# the project-specific scope to the target that should own it.
SCOPE_HOME = {
    "ffp": "ffp",
    "squirrel": "ffp",
    "ecs": "ffp",
    "archffp": "archffp",
    "pipeline": "archffp",
    "eval": "archffp",
}
# The two session stores — a project-scoped lesson living in either is misrouted.
SESSION_TARGETS = {"claude", "assistant"}


def find_misrouted(targets: dict | None = None) -> list[dict]:
    """Return lessons whose current store disagrees with their scope's home.

    A lesson is misrouted when it lives in a session store (claude/assistant)
    but carries a project-specific scope that now has a dedicated project store.
    Each result names where it is and where it should go, so the audit can be
    acted on mechanically (write to `suggested_target`, rm from `current_target`)."""
    targets = targets or TARGETS
    out: list[dict] = []
    for name in SESSION_TARGETS & set(targets):
        text = read_store(targets[name]["path"])
        for L in iter_lessons(text):
            home = SCOPE_HOME.get(L["scope"])
            if home and home != name and home in targets:
                out.append({
                    "slug": L["slug"],
                    "scope": L["scope"],
                    "current_target": name,
                    "suggested_target": home,
                    "trigger": L["trigger"],
                })
    return out


def cmd_audit(args) -> int:
    """Report lessons whose store disagrees with their scope's project home."""
    misrouted = find_misrouted()
    if not misrouted:
        print("no misrouted lessons — every project-scoped lesson is in its project store")
        return 0
    print(f"{len(misrouted)} misrouted lesson(s):")
    for m in misrouted:
        print(f"  [{m['scope']}] {m['slug']}: in {m['current_target']!r}, "
              f"belongs in {m['suggested_target']!r}")
        print(f"      → assistant-curator.py write --target {m['suggested_target']} "
              f"--scope {m['scope']} --slug {m['slug']} --trigger ... --rule ...")
        print(f"      → assistant-curator.py rm {m['slug']} --target {m['current_target']}")
    return 0


def cmd_write(args) -> int:
    tgt = TARGETS[args.target]
    trigger = args.trigger.strip()
    rule = args.rule.strip()
    if not trigger or not rule:
        print("ERROR: --trigger and --rule are required and non-empty.", file=sys.stderr)
        return 2
    default_scope = tgt["default_scope"]
    scope = args.scope or default_scope
    if scope not in tgt["scopes"]:
        print(
            f"ERROR: scope {scope!r} not valid for target {args.target!r}; "
            f"one of {sorted(tgt['scopes'])}",
            file=sys.stderr,
        )
        return 2
    slug = args.slug or slugify(f"{scope}-{trigger}" if scope != default_scope else trigger)
    added = getattr(args, "added", None) or today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", added):
        print(f"ERROR: --added must be YYYY-MM-DD, got {added!r}", file=sys.stderr)
        return 2

    tgt["path"].parent.mkdir(parents=True, exist_ok=True)
    text = read_store(tgt["path"])
    text = ensure_section(text, tgt.get("blurb"), tgt.get("preamble"))

    # Reject duplicate slug.
    for L in iter_lessons(text):
        if L["slug"] == slug:
            print(f"ERROR: lesson slug {slug!r} already exists.", file=sys.stderr)
            return 2

    block = (
        f"<!-- lesson: {slug}, scope: {scope}, added: {added} -->\n"
        f"**{trigger}**\n\n"
        f"{rule}\n\n"
    )
    bounds = find_section_bounds(text)
    assert bounds is not None
    insert_at = bounds[1]
    new_text = text[:insert_at] + block + text[insert_at:]
    write_store(tgt["path"], new_text)
    print(f"wrote lesson: {slug} → {tgt['path']}")
    # Sync to the cross-machine memory repo. Only CLAUDE.md lessons are mirrored
    # there (the 'assistant' target lives in the Observer prompt, not CLAUDE.md).
    if args.target == "claude":
        _sync_to_memory_repo()
    # Project targets live inside a repo — auto-commit the rules file so the
    # lesson travels with the code. Best-effort; never blocks the write.
    if "repo" in tgt:
        _commit_lesson_to_repo(tgt["repo"], tgt["path"], slug)
    return 0


def _commit_lesson_to_repo(repo_path: Path, rules_file: Path, slug: str) -> None:
    """Stage ONLY the rules file and commit it to its project repo.

    Best-effort and non-blocking: a failure (not a git repo, nothing to commit,
    pre-existing dirty tree elsewhere) is logged to the audit log and swallowed
    — the lesson is already written to disk; the commit is a convenience. We
    stage only `rules_file` so unrelated dirty changes in the repo are never
    swept into the lesson commit. `--no-verify` skips hooks (a lesson write must
    not be gated on the repo's pre-commit suite)."""
    try:
        rel = str(rules_file.relative_to(repo_path))
    except ValueError:
        rel = str(rules_file)
    try:
        add = subprocess.run(
            ["git", "-C", str(repo_path), "add", rel],
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            _audit(f"commit-lesson {slug}: git add failed: {add.stderr.strip()}")
            return
        commit = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", f"lesson: {slug}",
             "--no-verify", "--", rel],
            capture_output=True, text=True,
        )
        if commit.returncode != 0:
            _audit(f"commit-lesson {slug}: git commit failed: "
                   f"{(commit.stderr or commit.stdout).strip()}")
            return
        _audit(f"commit-lesson {slug}: committed {rel} to {repo_path}")
    except OSError as e:
        _audit(f"commit-lesson {slug}: {e}")


def _audit(msg: str) -> None:
    """One line to the Assistant's audit log. Never raises."""
    try:
        log = HOME / ".assistant" / "assistant-audit.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] "
                    f"assistant-curator: {msg}\n")
    except OSError:
        pass


def _sync_to_memory_repo() -> None:
    """Fire-and-forget push to the memory repo after a CLAUDE.md lesson write.

    Best-effort: imports the shared helper from bin/tools/ and never raises — a
    sync failure must not break `assistant-curator write`."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import memory_repo_sync  # noqa: PLC0415
        memory_repo_sync.sync_to_memory_repo("lesson")
    except Exception:  # noqa: BLE001
        pass


def cmd_list(args) -> int:
    # List across both stores (or just one if --target given).
    names = [args.target] if args.target else list(TARGETS)
    total = 0
    for name in names:
        text = read_store(TARGETS[name]["path"])
        lessons = list(iter_lessons(text))
        if args.scope:
            lessons = [L for L in lessons if L["scope"] == args.scope]
        if not lessons:
            continue
        print(f"[{name}] {TARGETS[name]['path']}")
        for L in lessons:
            print(f"  [{L['scope']:14}] {L['slug']:50} {L['added']}")
            print(f"           {L['trigger'][:90]}")
        total += len(lessons)
    if total == 0:
        print("(no lessons)")
    else:
        print(f"\n{total} lesson(s)")
    return 0


def cmd_rm(args) -> int:
    # Search both stores; remove from whichever holds the slug.
    names = [args.target] if args.target else list(TARGETS)
    for name in names:
        path = TARGETS[name]["path"]
        text = read_store(path)
        match = next((L for L in iter_lessons(text) if L["slug"] == args.slug), None)
        if match is None:
            continue
        new_text = text[:match["body_offset"]] + text[match["body_end"]:]
        new_text = re.sub(r"\n{3,}", "\n\n", new_text)
        write_store(path, new_text)
        print(f"removed lesson: {args.slug} (from {name})")
        return 0
    print(f"ERROR: no lesson with slug {args.slug!r}", file=sys.stderr)
    return 2


def cmd_trim(args) -> int:
    """Open the chosen store in $EDITOR. The user is the trim mechanism."""
    path = TARGETS[args.target]["path"]
    editor = os.environ.get("EDITOR", "vi")
    print(f"opening {path} in {editor}...")
    print(f"(go to '{SECTION_HEADING}' section. Delete blocks you don't want anymore.)")
    os.execvp(editor, [editor, str(path)])


def main() -> int:
    ap = argparse.ArgumentParser(prog="assistant-curator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    target_help = (
        "which lesson store: 'claude' (~/.claude/CLAUDE.md, every session), "
        "'assistant' (Observer prompt, orchestrator only), or a project store "
        "that lives in-repo and auto-commits: 'ffp' (firefly-platform), "
        "'archffp' (architect-ffp), 'assistant-repo' (this repo)")

    p_write = sub.add_parser("write", help="add a new lesson")
    p_write.add_argument("--trigger", required=True)
    p_write.add_argument("--rule", required=True)
    p_write.add_argument("--target", default=DEFAULT_TARGET, choices=sorted(TARGETS),
                         help=target_help)
    p_write.add_argument("--scope", default=None,
                         help="sub-domain within the target (see --target's allowed scopes)")
    p_write.add_argument("--slug", help="override the auto-generated slug")
    p_write.add_argument("--added", default=None,
                         help="override the added date (YYYY-MM-DD); used when "
                              "migrating an existing lesson to preserve its date")
    p_write.set_defaults(func=cmd_write)

    p_list = sub.add_parser("list", help="list lessons")
    p_list.add_argument("--target", default=None, choices=sorted(TARGETS),
                        help=target_help + " (default: both)")
    p_list.add_argument("--scope")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("rm", help="remove a lesson by slug")
    p_rm.add_argument("slug")
    p_rm.add_argument("--target", default=None, choices=sorted(TARGETS),
                      help=target_help + " (default: search both)")
    p_rm.set_defaults(func=cmd_rm)

    p_trim = sub.add_parser("trim", help="open a lesson store in $EDITOR for triage")
    p_trim.add_argument("--target", default=DEFAULT_TARGET, choices=sorted(TARGETS),
                        help=target_help)
    p_trim.set_defaults(func=cmd_trim)

    p_audit = sub.add_parser(
        "audit", help="report project-scoped lessons living in the wrong store")
    p_audit.set_defaults(func=cmd_audit)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
