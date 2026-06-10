#!/usr/bin/env python3
"""memory_seeds — mine the four memory categories from local sources.

Shared by obsidian-write --seed-all and mem0-add --seed-all so both backends
index the same grounded entries. Pure read-only: it reads CLAUDE.md, the warm
prompt, confirmed proposals, the action ledger, the observer report, the cmux
workspace list, and session transcripts, and returns plain dicts. It writes
nothing — the caller decides where each seed lands.

Each seed is:
    {"title": str, "body": str, "category": str, "tags": [str, ...],
     "frontmatter": {...}, "content": str}

`category` is one of: working_style | project | work_history | decision.
`content` is the one-line text a semantic backend (mem0) indexes; `title`/`body`
are what the Obsidian note uses. Both are populated for every seed so either
backend can consume the same list.

De-dup is the caller's job for mem0 (idempotent add) and obsidian-write's
never-overwrite suffixing; here we just produce a deterministic, ordered list.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
CLAUDE_MD = HOME / ".claude" / "CLAUDE.md"
PROPOSALS = HOME / ".assistant" / "proposals.jsonl"
LEDGER = HOME / ".assistant" / "actions-ledger.jsonl"
OBSERVER_REPORT = HOME / ".assistant" / "observer-latest-report.json"
TRANSCRIPT_DIR = HOME / ".claude" / "projects" / "-Users-mukuls-dev-assistant"


def _seed(title: str, body: str, category: str, tags: list[str],
          frontmatter: dict[str, Any], content: str | None = None) -> dict[str, Any]:
    return {"title": title, "body": body, "category": category,
            "tags": tags, "frontmatter": frontmatter,
            "content": content or f"{title}. {body}".strip()}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out = []
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


# ── 1. Working style ─────────────────────────────────────────────────────────

# Hand-curated distillations of how Mukul works, each grounded in a real source
# line. These are stable preferences, not transient state — the seed list is the
# canonical "working style" snapshot. Sources: CLAUDE.md, the warm prompt, and
# the confirmed-proposal corrections.
_WORKING_STYLE = [
    ("Prefers executive-summary replies — 2 bullets max, under 15 words each",
     "Response Style is an ABSOLUTE RULE in CLAUDE.md: cut all fluff and "
     "introductory phrases; answer like an executive summary.", "~/.claude/CLAUDE.md"),
    ("Never run destructive commands without explicit permission",
     "git checkout/reset/clean/restore, rm, force-push, rebase, stash drop — "
     "state what, where, and what's affected, then WAIT for approval. Verify "
     "pwd first. No exceptions.", "~/.claude/CLAUDE.md"),
    ("Always commit finished work — uncommitted work is lost work",
     "Commit Work is an ABSOLUTE RULE: never leave a finished unit of work "
     "uncommitted.", "~/.claude/CLAUDE.md"),
    ("Validate before calling anything done — exercise the real code path",
     "Compiles and tests-pass do NOT prove a feature works. UI: drive it in a "
     "browser and read back DOM. Bug fix: reproduce first, then confirm gone. "
     "If you can't validate, say so explicitly.", "~/.claude/CLAUDE.md"),
    ("Never send Slack messages on his behalf — draft and let him send",
     "Draft the message; Mukul sends it. The only exception is the FFP E2E "
     "reliability manager auto-pinging #munk-execution under tight conditions.",
     "~/.claude/CLAUDE.md"),
    ("Never expose credentials, tokens, or keys in plain text",
     "Don't grep secrets into shell commands or output; don't pass tokens as "
     "inline env vars; invoke the script that owns the secret rather than "
     "extracting it.", "~/.claude/CLAUDE.md"),
    ("Commit on decisions — act, don't re-offer a menu of options",
     "Decision Commitment is an ABSOLUTE RULE: an interrupt is the decision "
     "signal. 'just do it' / 'act' / 'pick the right one' mean cut the menu and "
     "execute.", "~/.claude/CLAUDE.md"),
    ("Move immediately into execution when he signals readiness",
     "'let's build' / 'go' / 'proceed' / 'ship it' → execute without "
     "re-summarizing or re-listing options. Stand by silently when nothing is "
     "actionable.", "confirmed proposal"),
    ("Wants terse replies, not bullet walls",
     "The assistant writes like someone who respects his time: short by "
     "default, more only when it earns it. Never surface workspace IDs or "
     "internal refs — translate to what actually happened.", "warm prompt"),
    ("Lead status updates with the work, push IDs to the end",
     "Morning briefs are a cognitive-burden ladder: lead each item with what "
     "the work is (project/feature), one sentence on what he must do, ws ref in "
     "brackets only as a locator.", "confirmed proposal + warm prompt"),
    ("'stuck?' is a status ping — answer in one line, never ask questions back",
     "Reply with either what you're blocked on, or one sentence on what you're "
     "doing next. 'Standing by' is acceptable. Never a long reply.",
     "confirmed proposal"),
    ("Pause and confirm before consequential or irreversible actions",
     "Anything that affects shared state, sends messages, or can't be undone: "
     "state the action and wait for an explicit yes. Don't assume implied "
     "consent.", "confirmed proposal"),
    ("Prefer simplest visibility surface first — local dashboard over Jira",
     "When asked to add visibility, propose a local dashboard / status file / "
     "structured log before external PM tools. Escalate only when multi-person "
     "coordination is confirmed.", "confirmed proposal"),
    ("uv for all Python; stdlib or well-maintained libs over hand-rolled code",
     "No test-only code in production paths. All imports at top of file. No "
     "comments explaining self-evident code. No defensive code without a real "
     "need.", "~/.claude/CLAUDE.md"),
    ("Markdown rules are reminders — always pair a lesson with a mechanical gate",
     "A rule in a file doesn't enforce anything. Pair it with a hook, CI check, "
     "eval cell, or settings.json gate. If none exists, say so and propose one.",
     "~/.claude/CLAUDE.md + confirmed proposal"),
    ("Save generated docs to ~/dev/generated-docs, hand HTML as file:// URLs",
     "Code reviews, READMEs, analysis reports live in ~/dev/generated-docs, not "
     "in the repo. HTML deliverables are always handed over as file:// absolute "
     "URLs so he can Cmd+click.", "~/.claude/CLAUDE.md"),
]


def working_style_seeds() -> list[dict[str, Any]]:
    out = []
    for title, body, source in _WORKING_STYLE:
        out.append(_seed(
            title=title, body=body, category="working_style",
            tags=["working-style", "preference"],
            frontmatter={"source": source},
            content=f"Mukul's working style: {title}. {body}"))
    return out


# ── 2. Project knowledge ─────────────────────────────────────────────────────

def _cmux_workspaces() -> dict[str, str]:
    """ref -> title, best-effort. Empty dict if cmux is down."""
    try:
        out = subprocess.run(["cmux", "list-workspaces", "--json"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        data = json.loads(out.stdout)
    except Exception:
        return {}
    return {w.get("ref", ""): (w.get("title") or "").strip()
            for w in data.get("workspaces", []) if w.get("ref")}


# Map a workspace-title keyword to a one-line domain description. Anything
# unmatched still gets a project note, just without the domain line.
_PROJECT_DOMAINS = [
    ("connections", "FFP Squirrel timeline — clip Connections feature (anchor "
     "lifecycle, auto-connect on freeform drop, delete cascade, move/trim parity)."),
    ("squirrel", "FFP Squirrel timeline editor — Lit web components + MobX, "
     "CSS tokens, E2E via Playwright."),
    ("probe", "FFP E2E reliability — probe runner that triages failing Squirrel "
     "specs and attributes breakages."),
    ("architect", "ArchitectFFP — the fix/feature pipeline orchestrator for "
     "firefly-platform work."),
    ("mem0", "Mem0 semantic memory backend for the Assistant — embeddings over "
     "transcripts and decisions."),
    ("obsidian", "Obsidian memory layer — structured human-readable notes of "
     "work, lessons, and decisions in ~/dev/obs-elitecoder."),
    ("receipt", "Work receipt system — audit trail written before a workspace "
     "is torn down (CI/reviewer/quality)."),
    ("lesson-extractor", "Lesson extractor — mines the action ledger and session "
     "transcripts for recurring corrections/confirmations."),
    ("dashboard", "Assistant Fleet dashboard — local kanban view of workspace "
     "state and what needs attention."),
    ("td-099", "td-099 — replicate Auto-pilot knowledge-capture for Squirrel "
     "(routing tables, eval harness, gesture triads)."),
    ("td099", "td-099 — replicate Auto-pilot knowledge-capture for Squirrel "
     "(routing tables, eval harness, gesture triads)."),
    ("openclaw", "OpenClaw / Hermes — assistant agent design exploration."),
    ("hermes", "OpenClaw / Hermes — assistant agent design exploration."),
    ("meeting recorder", "Local Meeting Recorder — design exploration."),
]


def _domain_for(title: str) -> str:
    low = title.lower()
    for key, desc in _PROJECT_DOMAINS:
        if key in low:
            return desc
    return ""


def _ledger_latest_by_ws(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        ws = r.get("ws_ref")
        if ws:
            latest[ws] = r  # rows are in file order -> ends on the newest
    return latest


def _observer_classifications() -> dict[str, str]:
    """ws_ref -> classification from the latest observer report."""
    try:
        data = json.loads(OBSERVER_REPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for action in data.get("candidate_actions", []) or []:
        ws = action.get("_source_ws") or (action.get("params") or {}).get("ws_ref")
        cls = action.get("_classification")
        if ws and cls and ws not in out:
            out[ws] = cls
    return out


def project_seeds() -> list[dict[str, Any]]:
    """One note per active workspace/project, grounded in cmux title + the
    workspace's newest ledger entry + observer classification."""
    workspaces = _cmux_workspaces()
    rows = _read_jsonl(LEDGER)
    latest = _ledger_latest_by_ws(rows)
    classifications = _observer_classifications()

    # Group by a normalized project name so two workspaces for the same project
    # (e.g. respawns) don't both spawn near-identical notes — keep the one with
    # the newest ledger activity.
    chosen: dict[str, tuple[str, str]] = {}  # project_name -> (ws_ref, title)
    for ref, title in workspaces.items():
        if not title:
            continue
        # Skip the warm-comms session and the manager — infrastructure, not a project.
        if "warm" in title.lower() or "assistant manager" in title.lower():
            continue
        name = _project_name(title)
        prev = chosen.get(name)
        if prev is None:
            chosen[name] = (ref, title)
        else:
            # keep whichever has newer ledger activity
            prev_ts = (latest.get(prev[0]) or {}).get("ts", "")
            cur_ts = (latest.get(ref) or {}).get("ts", "")
            if cur_ts > prev_ts:
                chosen[name] = (ref, title)

    out = []
    for name, (ref, title) in sorted(chosen.items()):
        last = latest.get(ref) or {}
        domain = _domain_for(title)
        cls = classifications.get(ref, "")
        evidence = (last.get("evidence") or "").strip()
        body_lines = []
        if domain:
            body_lines.append(domain)
        if cls:
            body_lines.append(f"\n- **Observer status:** {cls}")
        if last.get("ts"):
            body_lines.append(f"- **Last activity:** {last['ts'][:10]} "
                              f"({last.get('kind', '')})")
        if evidence:
            body_lines.append(f"- **Latest:** {evidence[:400]}")
        body = "\n".join(body_lines) or "Active workspace; no ledger detail yet."
        fm = {"workspace": ref}
        if cls:
            fm["status"] = cls
        out.append(_seed(
            title=name, body=body, category="project",
            tags=["project"] + ([cls.lower()] if cls else []),
            frontmatter=fm,
            content=f"Project {name}: {domain or title}. "
                    f"Status {cls or 'active'}. {evidence[:200]}"))
    return out


def _project_name(title: str) -> str:
    """Strip the trailing ' [NN]' locator and leading status glyphs from a cmux
    title to get a stable project name."""
    t = re.sub(r"\s*\[\d+\]\s*$", "", title).strip()
    t = re.sub(r"^[^\w]+", "", t).strip()  # drop leading ✳/�overline glyphs
    return t or title.strip()


# ── 3. Work history ──────────────────────────────────────────────────────────

# Ledger kinds that represent a real, verified outcome worth a history note.
_HISTORY_KINDS = {"emit-card"}
# Evidence stems that signal a completed deliverable rather than a routine card.
_DONE_SIGNALS = ("shipped", "committed", "pushed", "merged", "implemented",
                 "delivered", "ci green", "validated", "done")


def work_history_seeds(limit: int = 40) -> list[dict[str, Any]]:
    """Notes for verified, completed work, mined from the ledger tail. We keep
    the newest entry per (ws_ref) that carries a done-signal in its evidence, so
    the history is one entry per project's latest milestone rather than every
    pulse card."""
    rows = [r for r in _read_jsonl(LEDGER)
            if r.get("outcome") == "verified" and r.get("kind") in _HISTORY_KINDS]
    # newest-per-ws among done-signalled entries
    chosen: dict[str, dict[str, Any]] = {}
    for r in rows[-1200:]:
        ev = (r.get("evidence") or "").lower()
        if not any(sig in ev for sig in _DONE_SIGNALS):
            continue
        ws = r.get("ws_ref") or r.get("key", "")
        chosen[ws] = r  # file order -> newest wins
    workspaces = _cmux_workspaces()

    seeds = []
    for ws, r in chosen.items():
        title_src = workspaces.get(ws) or ws
        proj = _project_name(title_src)
        date = (r.get("ts") or "")[:10]
        evidence = (r.get("evidence") or "").strip()
        title = f"{proj} — {date}" if date else proj
        body = (f"- **Workspace:** {ws}\n"
                f"- **Verified:** {date} via {r.get('verified_via', 'observer')}\n\n"
                f"{evidence}")
        seeds.append(_seed(
            title=title, body=body, category="work_history",
            tags=["work-history", "verified"],
            frontmatter={"workspace": ws, "date": date} if date
            else {"workspace": ws},
            content=f"Work history — {proj} ({date}): {evidence[:300]}"))
    # newest first, capped
    seeds.sort(key=lambda s: s["frontmatter"].get("date", ""), reverse=True)
    return seeds[:limit]


# ── 4. Decisions ─────────────────────────────────────────────────────────────

_DECISION_PATTERNS = [
    re.compile(r"\b(?:decided|decide) to\b[^.\n]{0,160}", re.I),
    re.compile(r"\bgoing with\b[^.\n]{0,160}", re.I),
    re.compile(r"\bwe(?:'ll| will) use\b[^.\n]{0,160}", re.I),
    re.compile(r"\blet'?s go with\b[^.\n]{0,160}", re.I),
]
# A short, directive user message is itself a decision — these are the
# terse one-liners ("yes, lets ship this work", "Let's do both").
_DECISION_DIRECTIVE = re.compile(
    r"\b(let'?s|lets|go with|i want|i prefer|ship it|merge|stick with|"
    r"switch to|use mem0|do both|fix .* first|forget about|skip the|drop the)\b",
    re.I)
# Noise filter — skip matches that are clearly not Mukul's product/arch calls.
_DECISION_SKIP = ("approved=", "reviewdecision", "auto-merge", "approvals",
                  "the user explicitly approved", "read /users")


def _iter_user_texts(path: Path) -> Iterable[str]:
    """Yield user-turn text strings from one transcript jsonl."""
    try:
        fh = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                yield content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        yield part.get("text", "")


def decision_seeds(limit: int = 15, scan_files: int = 50) -> list[dict[str, Any]]:
    """Scan the most recent transcript files for decision signals in Mukul's
    own turns. De-duped by the normalized decision text."""
    if not TRANSCRIPT_DIR.exists():
        return []
    files = sorted(TRANSCRIPT_DIR.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:scan_files]
    seen: set[str] = set()
    seeds: list[dict[str, Any]] = []

    def _emit(frag: str, date: str) -> bool:
        """Add a decision seed; return True once the limit is reached."""
        frag = " ".join(frag.split()).strip()
        low = frag.lower()
        if not frag or any(s in low for s in _DECISION_SKIP):
            return False
        key = low[:80]
        if key in seen:
            return False
        seen.add(key)
        seeds.append(_seed(
            title=frag[:90].rstrip(" ,;:"),
            body=f"> {frag}\n\n- **Captured:** {date}\n"
                 f"- **Source:** session transcript",
            category="decision", tags=["decision"],
            frontmatter={"date": date, "source": "transcript"},
            content=f"Decision ({date}): {frag}"))
        return len(seeds) >= limit

    for f in files:
        date = _date_from_mtime(f)
        for text in _iter_user_texts(f):
            if len(text) > 2000:  # skip giant pasted specs
                continue
            # A short directive one-liner is the decision itself.
            stripped = " ".join(text.split()).strip()
            if 5 < len(stripped) < 220 and _DECISION_DIRECTIVE.search(stripped):
                if _emit(stripped, date):
                    return seeds
                continue
            # Otherwise look for a decision phrase embedded in prose.
            for pat in _DECISION_PATTERNS:
                for m in pat.finditer(text):
                    if _emit(m.group(0), date):
                        return seeds
    return seeds


def _date_from_mtime(path: Path) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


# ── confirmed lessons (backfill source, shared) ──────────────────────────────

def confirmed_lessons() -> list[dict[str, Any]]:
    """Confirmed lesson proposals, as seed dicts under the 'lesson' category.
    Used by both the Obsidian backfill and mem0 --seed-lessons."""
    out = []
    for e in _read_jsonl(PROPOSALS):
        if e.get("type") != "lesson" or e.get("status") != "confirmed":
            continue
        trigger = e.get("trigger", "") or "lesson"
        rule = e.get("rule", "")
        scope = e.get("scope") or "general"
        target = e.get("target") or "assistant"
        confirmed = e.get("confirmed_at") or e.get("ts") or ""
        body = (f"**Rule:** {rule}\n\n"
                f"- **Trigger:** {trigger}\n"
                f"- **Target store:** {target}\n"
                f"- **Scope:** {scope}\n"
                f"- **Confirmed:** {confirmed}\n"
                f"- **Source:** {e.get('source', 'manual')}")
        out.append(_seed(
            title=trigger[:120], body=body, category="lesson",
            tags=["lesson", scope, target],
            frontmatter={"target": target, "scope": scope,
                         "source": e.get("source", "manual")},
            content=f"Lesson — when {trigger}: {rule}"))
    return out


def all_seeds() -> dict[str, list[dict[str, Any]]]:
    """Every category in one call — the shape both --seed-all passes consume."""
    return {
        "working_style": working_style_seeds(),
        "project": project_seeds(),
        "work_history": work_history_seeds(),
        "decision": decision_seeds(),
    }


if __name__ == "__main__":
    # Diagnostic: print per-category counts so a human can eyeball the mine.
    counts = {k: len(v) for k, v in all_seeds().items()}
    counts["lesson(confirmed)"] = len(confirmed_lessons())
    print(json.dumps(counts, indent=2))
