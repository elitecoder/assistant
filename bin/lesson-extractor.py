#!/usr/bin/env python3
"""lesson-extractor — surface recurring action patterns as lesson proposals.

The Assistant's action ledger is a record of what it actually did. When the
same kind of thing happens over and over (same verdict kind + same evidence
shape), that repetition is a candidate rule: either a behavior to encode or a
mistake to stop making. This script detects those patterns and drafts a lesson
proposal the user can confirm with a single `y`.

Pipeline:
  1. Tail the last 200 ledger entries.
  2. Group by (kind, evidence_stem), where evidence_stem = first 8 words of
     the evidence, lowercased, stripped of workspace refs / PR numbers.
  3. A group with >= 3 verified outcomes inside a 72h window is a candidate.
  4. Drop candidates whose stem matches an existing lesson trigger (curator
     list, both targets) OR a still-pending proposal in proposals.jsonl
     (idempotency — running twice produces no duplicates).
  5. For each surviving candidate, ask Claude (one-shot `claude -p`) to write a
     {trigger, rule, target, scope}.
  6. Append the proposal to proposals.jsonl (atomic single-line append).
  7. Ping the user via tg-send.py so they can reply `y`.

Every proposal written OR skipped logs one line to assistant-audit.log. Safe to
run from pulse.py (hourly) or standalone. --dry-run prints what it would
propose and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
HOME = Path.home()

ASSISTANT_DIR = HOME / ".assistant"
LEDGER_PATH = ASSISTANT_DIR / "actions-ledger.jsonl"
PROPOSALS_PATH = ASSISTANT_DIR / "comms" / "proposals.jsonl"
AUDIT_LOG = ASSISTANT_DIR / "assistant-audit.log"
CURATOR = BIN / "assistant-curator.py"
TG_SEND = BIN / "tg-send.py"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))
EXTRACTOR_MODEL = os.environ.get(
    "EXTRACTOR_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")

# Tunables.
TAIL_N = 200
MIN_OCCURRENCES = 3
WINDOW_SEC = 72 * 3600
STEM_WORDS = 8

# Strip these noise tokens out of evidence before computing the stem so the
# grouping keys on the SHAPE of the evidence, not its per-workspace specifics.
_WS_REF_RE = re.compile(r"\b(?:workspace|ws|surface|pane|window):\d+", re.IGNORECASE)
_PR_RE = re.compile(r"#\d+")
_TD_RE = re.compile(r"\btd-\d+", re.IGNORECASE)
_NUM_RE = re.compile(r"\b\d+\b")
_WS_COLLAPSE_RE = re.compile(r"\s+")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_us() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_epoch() -> int:
    import time
    return int(time.time())


# ─── audit log ────────────────────────────────────────────────────────────

def audit(msg: str) -> None:
    """One line to assistant-audit.log. Never raises — a logging failure must
    not abort extraction."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(f"[{utc_iso()}] lesson-extractor: {msg}\n")
    except OSError:
        pass


# ─── ledger reading + pattern detection (pure) ──────────────────────────────

def read_ledger_tail(path: Path = LEDGER_PATH, n: int = TAIL_N) -> list[dict[str, Any]]:
    """Last n well-formed JSON entries from the ledger, oldest-first."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
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


def evidence_stem(evidence: str, words: int = STEM_WORDS) -> str:
    """First `words` words of evidence, lowercased, with workspace refs / PR
    numbers / td ids / bare numbers stripped. This is the grouping key — it
    collapses 'sent /cleanup to workspace:4 delta=12' and 'sent /cleanup to
    workspace:90 delta=8' to the same stem."""
    text = (evidence or "").lower()
    text = _WS_REF_RE.sub(" ", text)
    text = _PR_RE.sub(" ", text)
    text = _TD_RE.sub(" ", text)
    text = _NUM_RE.sub(" ", text)
    text = _WS_COLLAPSE_RE.sub(" ", text).strip()
    return " ".join(text.split()[:words])


def find_patterns(entries: list[dict[str, Any]], *, now: int,
                  min_occurrences: int = MIN_OCCURRENCES,
                  window_sec: int = WINDOW_SEC) -> list[dict[str, Any]]:
    """Group verified entries by (kind, evidence_stem); return groups with
    >= min_occurrences inside the trailing window. Each candidate carries the
    kind, stem, count, a representative evidence sample, and the ws_refs seen.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if e.get("outcome") != "verified":
            continue
        epoch = e.get("epoch")
        if isinstance(epoch, (int, float)) and now - epoch > window_sec:
            continue
        kind = e.get("kind") or ""
        stem = evidence_stem(e.get("evidence") or "")
        if not kind or not stem:
            continue
        groups[(kind, stem)].append(e)

    candidates: list[dict[str, Any]] = []
    for (kind, stem), members in groups.items():
        if len(members) < min_occurrences:
            continue
        ws_refs = sorted({m.get("ws_ref") for m in members if m.get("ws_ref")})
        samples = [m.get("evidence", "") for m in members[:3]]
        candidates.append({
            "kind": kind,
            "stem": stem,
            "count": len(members),
            "ws_refs": ws_refs,
            "samples": samples,
        })
    # Most-frequent first — the strongest patterns lead.
    candidates.sort(key=lambda c: c["count"], reverse=True)
    return candidates


# ─── dedup against existing lessons + pending proposals ─────────────────────

def _norm(text: str) -> str:
    """Loose normalization for trigger/stem overlap checks."""
    return _WS_COLLAPSE_RE.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def existing_triggers(curator: Path = CURATOR,
                      runner: Callable[[list[str]], tuple[int, str, str]] | None = None) -> list[str]:
    """All known lesson triggers across both stores, via the curator list CLI.
    Returns [] if the curator is unavailable — dedup degrades to 'allow', which
    is safe because the user still has to confirm every proposal."""
    runner = runner or _run
    triggers: list[str] = []
    for target in ("assistant", "claude"):
        rc, out, _ = runner([sys.executable, str(curator), "list", "--target", target])
        if rc != 0:
            continue
        # `list` prints a human table; trigger text is the indented line under
        # each "[scope] slug added" row. Capture any line that isn't a header.
        for line in out.splitlines():
            s = line.strip()
            if not s or s.startswith("[") or s.endswith("lesson(s)") or s.startswith("("):
                continue
            triggers.append(s)
    return triggers


def pending_proposal_stems(path: Path = PROPOSALS_PATH) -> set[str]:
    """Evidence-equivalent stems already represented by a pending OR confirmed
    lesson proposal. Both block a re-propose: pending → user hasn't answered;
    confirmed → it's already a lesson. We key on the trigger's stem so an
    extractor proposal and a later one for the same pattern collapse."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return set()
    stems: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "lesson":
            continue
        if obj.get("status") not in ("pending", "confirmed"):
            continue
        # Prefer the stored stem (extractor writes it); fall back to the trigger.
        stem = obj.get("pattern_stem") or evidence_stem(obj.get("trigger", ""))
        if stem:
            stems.add(_norm(stem))
    return stems


def is_duplicate(candidate: dict[str, Any], triggers: list[str],
                 pending_stems: set[str]) -> bool:
    """True if this pattern is already covered by a lesson or a live proposal."""
    stem_norm = _norm(candidate["stem"])
    if stem_norm in pending_stems:
        return True
    # Substring overlap either direction: an existing trigger that contains the
    # stem, or a stem that contains the trigger.
    for trig in triggers:
        tn = _norm(trig)
        if not tn:
            continue
        if stem_norm and (stem_norm in tn or tn in stem_norm):
            return True
    return False


# ─── LLM draft ──────────────────────────────────────────────────────────────

DRAFT_PROMPT = """\
You are mining an AI assistant's action log for rules worth encoding.

A recurring pattern was detected. The assistant performed the same kind of \
action ({kind}) with the same evidence shape {count} times in the last 72 \
hours.

Representative evidence samples:
{samples}

Write ONE lesson for the assistant's Observer (the component that decides what \
to do with each workspace). The lesson is a rule: either codify a good behavior \
this pattern reflects, or prevent a mistake it reveals.

Return ONLY a single JSON object, no prose, no code fences:
{{"trigger": "<one short line naming when the rule fires>", \
"rule": "<one paragraph, imperative, what to do and why>", \
"target": "assistant", \
"scope": "<one of: verdict, merge, cleanup, stranded, general>"}}
"""


def build_draft_prompt(candidate: dict[str, Any]) -> str:
    samples = "\n".join(f"  - {s[:200]}" for s in candidate["samples"])
    return DRAFT_PROMPT.format(
        kind=candidate["kind"], count=candidate["count"], samples=samples)


def _run(cmd: list[str], timeout: int = 120,
         env: dict[str, str] | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _bedrock_env() -> dict[str, str]:
    """Merge ~/.zprofile Bedrock vars onto the env for the headless claude call
    (launchd doesn't source zprofile). Reuses comms_lib's parser."""
    env = dict(os.environ)
    try:
        sys.path.insert(0, str(BIN))
        import comms_lib  # noqa: PLC0415
        for k, v in comms_lib.load_bedrock_env().items():
            env.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    return env


def draft_lesson(candidate: dict[str, Any],
                 llm: Callable[[str], str] | None = None) -> dict[str, Any] | None:
    """Ask the LLM to turn a candidate pattern into {trigger, rule, target,
    scope}. Returns None if the call fails or the output isn't usable JSON.
    `llm` is injectable for tests; the default shells out to `claude -p`."""
    prompt = build_draft_prompt(candidate)
    raw = (llm or _claude_oneshot)(prompt)
    if not raw:
        return None
    obj = _extract_json(raw)
    if not obj:
        return None
    trigger = (obj.get("trigger") or "").strip()
    rule = (obj.get("rule") or "").strip()
    if not trigger or not rule:
        return None
    return {
        "trigger": trigger,
        "rule": rule,
        "target": (obj.get("target") or "assistant").strip() or "assistant",
        "scope": (obj.get("scope") or "general").strip() or "general",
    }


def _claude_oneshot(prompt: str) -> str:
    rc, out, _ = _run(
        [CLAUDE_BIN, "--model", EXTRACTOR_MODEL, "--print", prompt],
        timeout=120, env=_bedrock_env())
    return out if rc == 0 else ""


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of an LLM reply, tolerating code fences
    and surrounding prose."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ─── proposal write + ping ───────────────────────────────────────────────────

def write_proposal(draft: dict[str, Any], candidate: dict[str, Any],
                   path: Path = PROPOSALS_PATH) -> str:
    """Append one pending proposal line. Returns its id. Atomic: the full dict
    is built then written as one line."""
    ts = utc_iso_us()
    entry = {
        "ts": ts,
        "id": ts,
        "type": "lesson",
        "target": draft["target"],
        "trigger": draft["trigger"],
        "rule": draft["rule"],
        "scope": draft["scope"],
        "source": "extractor",
        "status": "pending",
        # Provenance + the dedup key for future runs.
        "pattern_stem": candidate["stem"],
        "pattern_kind": candidate["kind"],
        "pattern_count": candidate["count"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return ts


def ping_user(trigger: str, tg_send: Path = TG_SEND,
              runner: Callable[[list[str]], tuple[int, str, str]] | None = None) -> bool:
    runner = runner or _run
    body = (f"Lesson proposal from pattern: {trigger}. "
            "Reply y in the main chat to add it.")
    rc, _, _ = runner([sys.executable, str(tg_send), "--text", body,
                       "--kind", "action"])
    return rc == 0


# ─── orchestration ───────────────────────────────────────────────────────────

def extract(*, dry_run: bool = False,
            llm: Callable[[str], str] | None = None,
            ledger_path: Path = LEDGER_PATH,
            proposals_path: Path = PROPOSALS_PATH,
            curator: Path = CURATOR,
            tg_send: Path = TG_SEND,
            now: int | None = None) -> dict[str, Any]:
    """Run one extraction pass. Returns a summary dict.

    Designed for both pulse.py (defaults) and tests (inject llm + paths + now).
    """
    now = now if now is not None else now_epoch()
    entries = read_ledger_tail(ledger_path)
    candidates = find_patterns(entries, now=now)

    triggers = existing_triggers(curator)
    pending = pending_proposal_stems(proposals_path)

    proposed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for cand in candidates:
        if is_duplicate(cand, triggers, pending):
            audit(f"skipped pattern kind={cand['kind']!r} stem={cand['stem']!r} "
                  f"count={cand['count']} (already a lesson/proposal)")
            skipped.append({"reason": "duplicate", **cand})
            continue

        draft = draft_lesson(cand, llm=llm)
        if not draft:
            audit(f"skipped pattern kind={cand['kind']!r} stem={cand['stem']!r} "
                  f"count={cand['count']} (LLM produced no usable draft)")
            skipped.append({"reason": "no-draft", **cand})
            continue

        if dry_run:
            audit(f"[dry-run] would propose trigger={draft['trigger']!r} "
                  f"from kind={cand['kind']!r} stem={cand['stem']!r} count={cand['count']}")
            proposed.append({"dry_run": True, "draft": draft, "candidate": cand})
            # In dry-run, register the stem locally so two identical candidates
            # in one pass don't both "propose".
            pending.add(_norm(cand["stem"]))
            continue

        pid = write_proposal(draft, cand, proposals_path)
        pinged = ping_user(draft["trigger"], tg_send)
        audit(f"proposed id={pid} trigger={draft['trigger']!r} "
              f"from kind={cand['kind']!r} stem={cand['stem']!r} "
              f"count={cand['count']} pinged={pinged}")
        proposed.append({"id": pid, "draft": draft, "candidate": cand,
                         "pinged": pinged})
        pending.add(_norm(cand["stem"]))

    return {
        "n_entries": len(entries),
        "n_candidates": len(candidates),
        "n_proposed": len(proposed),
        "n_skipped": len(skipped),
        "proposed": proposed,
        "skipped": skipped,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Detect recurring patterns in the action ledger and draft "
                    "lesson proposals the user can confirm.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print proposals without writing them or pinging.")
    args = ap.parse_args(argv)

    result = extract(dry_run=args.dry_run)

    if args.dry_run:
        # Human-readable summary to stdout in dry-run.
        print(json.dumps({
            "n_entries": result["n_entries"],
            "n_candidates": result["n_candidates"],
            "would_propose": [
                {"trigger": p["draft"]["trigger"], "rule": p["draft"]["rule"],
                 "target": p["draft"]["target"], "scope": p["draft"]["scope"],
                 "pattern": {"kind": p["candidate"]["kind"],
                             "count": p["candidate"]["count"],
                             "stem": p["candidate"]["stem"]}}
                for p in result["proposed"]
            ],
            "skipped": [{"reason": s["reason"], "kind": s["kind"],
                         "stem": s["stem"], "count": s["count"]}
                        for s in result["skipped"]],
        }, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({k: result[k] for k in
                          ("n_entries", "n_candidates", "n_proposed", "n_skipped")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
