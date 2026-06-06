#!/usr/bin/env python3
"""lesson-extractor — surface recurring patterns as lesson proposals.

Two signal sources feed one proposal pipeline:

  Pass 1 — the action ledger. A record of what the Assistant actually did. When
  the same kind of thing happens over and over (same verdict kind + same
  evidence shape), that repetition is a candidate rule.

  Pass 2 — Claude Code session transcripts (~/.claude/projects/*/*.jsonl). Every
  correction, confirmation, and repeated question the user ever made lives
  there — the richest lesson signal available. The TranscriptScanner reads a
  bounded, recency-prioritized slice, isolates genuine human turns (stripping
  tool results, harness reminders, and spawn prompts), and emits candidates in
  the same shape the ledger pass produces. Skipped with --ledger-only.

Both passes drop candidates already covered by an existing lesson or a pending
proposal (idempotency — running twice produces no duplicates), ask Claude
(one-shot `claude -p`) to write a {trigger, rule, target, scope}, append it to
proposals.jsonl (atomic single-line append), and ping the user via tg-send.py so
they can reply `y`.

Every proposal written OR skipped logs one line to assistant-audit.log. Safe to
run from pulse.py or standalone. --dry-run prints what it would propose and
writes nothing; --ledger-only skips the transcript scan for fast pulse runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
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

# Where Claude Code writes one JSONL transcript per session.
CLAUDE_PROJECTS = HOME / ".claude" / "projects"

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


# ─── transcript scanning ─────────────────────────────────────────────────────

# A user turn carries a correction when it opens with / contains one of these.
# Patterns are deliberately specific: bare "no"/"don't" anywhere over-matches
# normal instructions, so "no" is anchored to message start and the rest target
# correction-shaped phrasing. The user still confirms every proposal, so a stray
# match costs one ignorable ping, not a bad rule.
_CORRECTION_PATTERNS = [
    re.compile(r"^\s*no\b[,.\s]", re.I),
    re.compile(r"^\s*nope\b", re.I),
    re.compile(r"\bdon'?t\b", re.I),
    re.compile(r"\bdo not\b", re.I),
    re.compile(r"\bstop\b", re.I),
    re.compile(r"\bwrong\b", re.I),
    re.compile(r"\bthat'?s not\b", re.I),
    re.compile(r"\bthat is not\b", re.I),
    re.compile(r"\bnot what i (asked|meant|wanted|said)\b", re.I),
    re.compile(r"\bi told you\b", re.I),
    re.compile(r"\byou keep\b", re.I),
    re.compile(r"\byou (keep|always) \w+ing\b", re.I),
    re.compile(r"\bnever (do|send|run|use|touch) that\b", re.I),
    re.compile(r"\bnever do that\b", re.I),
    re.compile(r"\bi said\b", re.I),
    re.compile(r"\bwhy (did|are) you\b", re.I),
    re.compile(r"\bactually,", re.I),
]

# A user turn confirms a behavior when it contains praise / explicit assent.
_CONFIRMATION_PATTERNS = [
    re.compile(r"\byes,?\s+(exactly|perfect|please|that)\b", re.I),
    re.compile(r"\bexactly\b", re.I),
    re.compile(r"\bperfect\b", re.I),
    re.compile(r"\bkeep doing\b", re.I),
    re.compile(r"\blove (it|that|this)\b", re.I),
    re.compile(r"\bthat'?s right\b", re.I),
    re.compile(r"\bnailed it\b", re.I),
    re.compile(r"\b(great|good) (job|work|call|catch|idea|point)\b", re.I),
    re.compile(r"\bawesome\b", re.I),
    re.compile(r"\bbeautiful\b", re.I),
    re.compile(r"^\s*(yes|yep|yeah)[!.]", re.I),
    re.compile(r"\bthat'?s perfect\b", re.I),
]

# Telegram-relayed user turns are prefixed with routing metadata; strip it to
# recover the human's actual words.
_TELEGRAM_PREFIX_RE = re.compile(r"^\[telegram[^\]]*\]\s*", re.I)

# System-injected "user" turns we must NOT mistake for the human: tool results,
# harness reminders, spawn prompts (JSON context blobs starting with { or [),
# local-command echoes, interrupts, and "Read <path>.md and execute it" prompts.
_SYSTEM_TURN_RE = re.compile(
    r"^\s*(?:[<{\[]|caveat:|\[request interrupted|api error|"
    r"read /users/\S+\.md|read the file|you are |your task|"
    r"this session is being continued|please continue|"
    r"the user(?:'s)? (?:sent|previous|original))",
    re.I,
)


def signal_stem(text: str, words: int = 10) -> str:
    """First `words` normalized words of a correction/confirmation — the key
    used to collapse the same human signal across sessions and to fuzzy-match
    against existing lessons."""
    return " ".join(_norm(text).split()[:words])


class TranscriptScanner:
    """Scans Claude Code session transcripts for correction/confirmation signals.

    Claude Code writes one JSONL transcript per session under
    ~/.claude/projects/<slug>/<uuid>.jsonl. Every correction, confirmation, and
    repeated question the user ever made lives there — a far richer lesson signal
    than the action ledger. This scanner reads a bounded, recency-prioritized
    slice of those transcripts, isolates genuine human turns (stripping tool
    results, harness reminders, and spawn prompts), and emits candidate lessons
    in the same shape the ledger extractor produces, so they flow through the
    identical dedup → draft → propose pipeline.
    """

    # Bounds — keep a single run fast and memory-safe. Tuned, not magic.
    MAX_FILES = 500            # newest-by-mtime cap across all scanned dirs
    MAX_FILE_BYTES = 5 * 1024 * 1024  # skip huge transcripts (CI/build logs)
    MTIME_WINDOW_DAYS = 90     # only recent sessions carry current behavior
    PER_FILE_TIMEOUT_SEC = 5.0  # abort a file that parses too slowly
    MAX_CANDIDATES = 8         # cap proposals per run — each one pings the user
    MIN_RECURRING_SESSIONS = 3  # a question must recur in N distinct sessions
    LINE_TIME_CHECK_EVERY = 500  # re-check the per-file deadline this often

    # Highest-signal project dirs. The assistant's own sessions lead; work
    # sessions follow. Everything else (eval sweeps, worktrees) is skipped.
    PRIORITY_DIRS = (
        "-Users-mukuls-dev-assistant",
        "-Users-mukuls-dev-firefly-platform",
    )

    def __init__(self, roots: list[Path] | None = None,
                 projects_dir: Path = CLAUDE_PROJECTS,
                 existing: list[str] | None = None,
                 now: float | None = None):
        if roots is None:
            roots = [projects_dir / d for d in self.PRIORITY_DIRS
                     if (projects_dir / d).is_dir()]
        self.roots = roots
        self.now = now if now is not None else time.time()
        # Existing lesson triggers for fuzzy dedup. Fetched lazily by the caller
        # and injected; default empty (dedup degrades to allow, still gated).
        self._existing_stems = {signal_stem(t) for t in (existing or []) if t}

    # ── file selection ──────────────────────────────────────────────────────

    def _gather_files(self) -> list[Path]:
        """Recency-sorted, filtered transcript paths under the scan roots."""
        cutoff = self.now - self.MTIME_WINDOW_DAYS * 86400
        scored: list[tuple[float, Path]] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for p in root.glob("*.jsonl"):
                sp = str(p)
                if "/tmp" in sp or "spawn-prompts" in sp:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size > self.MAX_FILE_BYTES:
                    continue
                if st.st_mtime < cutoff:
                    continue
                scored.append((st.st_mtime, p))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [p for _, p in scored[:self.MAX_FILES]]

    # ── turn-text extraction ──────────────────────────────────────────────────

    @staticmethod
    def _user_text(content: Any) -> str | None:
        """The human's words from a user turn, or None if it's a tool result or
        a system-injected turn (harness reminder, spawn prompt, interrupt)."""
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(p for p in parts if p).strip()
            if not text:  # tool_result-only / image-only turn
                return None
        elif isinstance(content, str):
            text = content
        else:
            return None
        text = _TELEGRAM_PREFIX_RE.sub("", text).replace("\r", " ").strip()
        if not text or _SYSTEM_TURN_RE.match(text):
            return None
        return text

    @staticmethod
    def _assistant_text(content: Any) -> str | None:
        """The assistant's visible reply (text blocks only — thinking and
        tool_use are dropped)."""
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(p for p in parts if p).strip()
            return text or None
        return None

    def _conversation(self, path: Path) -> list[tuple[str, str]]:
        """Ordered (role, text) turns from one transcript, human/assistant only.
        Aborts (returns what it has) if parsing blows the per-file deadline."""
        deadline = time.monotonic() + self.PER_FILE_TIMEOUT_SEC
        turns: list[tuple[str, str]] = []
        try:
            f = open(path, "r", errors="replace")
        except OSError:
            return turns
        with f:
            for i, line in enumerate(f):
                if i % self.LINE_TIME_CHECK_EVERY == 0 and time.monotonic() > deadline:
                    audit(f"transcript scan: aborted slow file {path.name} "
                          f"after {self.PER_FILE_TIMEOUT_SEC}s")
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # malformed line — skip, never crash
                if not isinstance(obj, dict):
                    continue
                # isMeta: harness-injected content (slash-command skill bodies,
                # local-command echoes) — never the human's words.
                # isSidechain: a Task-tool subagent conversation, whose "user"
                # turn is an orchestrator prompt, not the operator.
                if obj.get("isMeta") or obj.get("isSidechain"):
                    continue
                role = obj.get("type")
                if role not in ("user", "assistant"):
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                text = (self._user_text(content) if role == "user"
                        else self._assistant_text(content))
                if text:
                    turns.append((role, text))
        return turns

    # ── signal detection ──────────────────────────────────────────────────────

    @staticmethod
    def _classify(text: str) -> str | None:
        """'correction' | 'confirmation' | None. Correction wins ties."""
        for rx in _CORRECTION_PATTERNS:
            if rx.search(text):
                return "correction"
        for rx in _CONFIRMATION_PATTERNS:
            if rx.search(text):
                return "confirmation"
        return None

    @staticmethod
    def _prev_assistant(turns: list[tuple[str, str]], i: int) -> str:
        """Nearest assistant turn before index i (what was corrected/confirmed)."""
        for j in range(i - 1, -1, -1):
            if turns[j][0] == "assistant":
                return turns[j][1]
        return ""

    # ── scan ────────────────────────────────────────────────────────────────

    def scan(self) -> list[dict[str, Any]]:
        """Return up to MAX_CANDIDATES transcript candidates, recurrence-first,
        deduped against existing lessons. Logs one audit summary line."""
        files = self._gather_files()
        # Aggregate correction/confirmation by (signal, stem) so the same human
        # signal across many sessions becomes one high-count candidate.
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        # Recurring questions: stem → set of distinct session files.
        q_files: dict[str, set[str]] = defaultdict(set)
        q_repr: dict[str, str] = {}
        n_signals = 0

        for path in files:
            turns = self._conversation(path)
            for i, (role, text) in enumerate(turns):
                if role != "user":
                    continue
                if text.rstrip().endswith("?") and len(text) <= 200:
                    qstem = signal_stem(text, words=8)
                    if qstem:
                        q_files[qstem].add(path.name)
                        q_repr.setdefault(qstem, text)
                sig = self._classify(text)
                if not sig:
                    continue
                n_signals += 1
                stem = signal_stem(text)
                if not stem:
                    continue
                key = (sig, stem)
                rec = agg.get(key)
                if rec is None:
                    agg[key] = {
                        "type": "transcript",
                        "signal": sig,
                        "source_file": path.name,
                        "assistant_context": self._prev_assistant(turns, i)[:200],
                        "user_signal": text[:150],
                        "stem": stem,
                        "kind": f"transcript:{sig}",
                        "count": 1,
                    }
                else:
                    rec["count"] += 1

        candidates = list(agg.values())
        # Recurring-question candidates: same stem in >= N distinct sessions.
        for qstem, sessions in q_files.items():
            if len(sessions) < self.MIN_RECURRING_SESSIONS:
                continue
            candidates.append({
                "type": "transcript",
                "signal": "recurring_question",
                "source_file": sorted(sessions)[0],
                "assistant_context": "",
                "user_signal": q_repr[qstem][:150],
                "stem": qstem,
                "kind": "transcript:recurring_question",
                "count": len(sessions),
            })

        # Drop anything a current lesson already covers (fuzzy first-10-words).
        kept: list[dict[str, Any]] = []
        for c in candidates:
            cs = c["stem"]
            if any(cs and (cs in es or es in cs) for es in self._existing_stems):
                continue
            kept.append(c)

        kept.sort(key=lambda c: c["count"], reverse=True)
        kept = kept[:self.MAX_CANDIDATES]
        audit(f"transcript scan: scanned {len(files)} files, found {n_signals} "
              f"signals, emitted {len(kept)} candidates")
        return kept


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


TRANSCRIPT_CORRECTION_PROMPT = """\
You are mining a developer's Claude Code session transcripts for rules worth \
encoding so the assistant stops repeating mistakes.

In {count} session turn(s) the user pushed back on the assistant. A \
representative exchange:

  ASSISTANT DID: {assistant_context}
  USER SAID: {user_signal}

Write ONE durable rule the assistant should follow so this correction never \
needs repeating. State what to do (and what to stop doing) and why. Generalize \
from the specific exchange — capture the underlying rule, not this one instance.

Pick the target store:
  - "claude": rules EVERY coding session must obey (general coding/workflow \
behavior). Valid scopes: global, classification, dashboard, ffp, scout, memory, \
security.
  - "assistant": rules ONLY for the orchestrator that decides what to do with \
each workspace (merge/cleanup/verdict policy). Valid scopes: verdict, merge, \
cleanup, stranded, general.

If the correction is about general coding or tool behavior, prefer "claude". \
If it is about orchestration/workspace verdicts, use "assistant".

Return ONLY a single JSON object, no prose, no code fences:
{{"trigger": "<one short line naming when the rule fires>", \
"rule": "<one paragraph, imperative, what to do and why>", \
"target": "<claude|assistant>", \
"scope": "<a valid scope for the chosen target>"}}

If the exchange is too vague to yield a real rule, return {{"skip": true}}.
"""

TRANSCRIPT_CONFIRMATION_PROMPT = """\
You are mining a developer's Claude Code session transcripts for behaviors \
worth reinforcing.

In {count} session turn(s) the user explicitly approved how the assistant \
worked. A representative exchange:

  ASSISTANT DID: {assistant_context}
  USER SAID: {user_signal}

Write ONE rule that reinforces this behavior so the assistant keeps doing it. \
Generalize the underlying good practice, not this one instance.

Pick the target store:
  - "claude": rules EVERY coding session must obey. Valid scopes: global, \
classification, dashboard, ffp, scout, memory, security.
  - "assistant": rules ONLY for the orchestrator (merge/cleanup/verdict \
policy). Valid scopes: verdict, merge, cleanup, stranded, general.

Return ONLY a single JSON object, no prose, no code fences:
{{"trigger": "<one short line naming when the rule fires>", \
"rule": "<one paragraph, imperative, what to do and why>", \
"target": "<claude|assistant>", \
"scope": "<a valid scope for the chosen target>"}}

If the exchange is too vague to yield a real rule, return {{"skip": true}}.
"""

TRANSCRIPT_QUESTION_PROMPT = """\
You are mining a developer's Claude Code session transcripts. The user asked \
the SAME question in {count} different sessions:

  QUESTION: {user_signal}

A question asked this often signals missing context the assistant should record \
once so it never has to be re-answered. Write ONE rule capturing the answer or \
the standing context, targeting "claude" (scope: global) unless it is clearly \
orchestration policy (target "assistant").

Return ONLY a single JSON object, no prose, no code fences:
{{"trigger": "<one short line naming when this comes up>", \
"rule": "<one paragraph stating the standing answer/context and what to do>", \
"target": "<claude|assistant>", \
"scope": "<a valid scope for the chosen target>"}}

If you cannot state a real answer, return {{"skip": true}}.
"""

# Valid scopes per target, mirrored from assistant-curator.py. Used to coerce an
# LLM scope choice onto something the curator will accept at confirm time.
_TARGET_SCOPES = {
    "claude": ({"global", "classification", "dashboard", "ffp", "scout",
                "memory", "security"}, "global"),
    "assistant": ({"verdict", "merge", "cleanup", "stranded", "general"},
                  "general"),
}


def build_draft_prompt(candidate: dict[str, Any]) -> str:
    if candidate.get("type") == "transcript":
        sig = candidate.get("signal")
        tmpl = {
            "correction": TRANSCRIPT_CORRECTION_PROMPT,
            "confirmation": TRANSCRIPT_CONFIRMATION_PROMPT,
            "recurring_question": TRANSCRIPT_QUESTION_PROMPT,
        }.get(sig, TRANSCRIPT_CORRECTION_PROMPT)
        return tmpl.format(
            count=candidate.get("count", 1),
            assistant_context=candidate.get("assistant_context", "") or "(none)",
            user_signal=candidate.get("user_signal", ""))
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
    if obj.get("skip"):  # LLM judged the signal too vague to encode.
        return None
    trigger = (obj.get("trigger") or "").strip()
    rule = (obj.get("rule") or "").strip()
    if not trigger or not rule:
        return None
    target = (obj.get("target") or "assistant").strip() or "assistant"
    scope = (obj.get("scope") or "").strip()
    if target not in _TARGET_SCOPES:
        target = "assistant"
    valid_scopes, default_scope = _TARGET_SCOPES[target]
    if scope not in valid_scopes:
        scope = default_scope
    return {"trigger": trigger, "rule": rule, "target": target, "scope": scope}


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
    # Transcript candidates carry which signal + source session they came from.
    if candidate.get("type") == "transcript":
        entry["source"] = "extractor-transcript"
        entry["pattern_signal"] = candidate.get("signal")
        entry["source_file"] = candidate.get("source_file")
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


# ─── cmux-watcher pattern feedback + discovery ───────────────────────────────
#
# The cmux-watcher (bin/cmux-watcher.py) pings the phone the instant a workspace
# needs input or finishes a notable turn, matching pattern_bank.json. Two
# learning loops close around it, both wired through this extractor:
#
#   1. Noise feedback — when a transcript correction is about notification
#      behavior ("that wasn't worth pinging me for", "stop sending me X"), call
#      pattern-feedback.py with feedback=noise for the matching pattern, so a
#      noisy pattern eventually mutes itself.
#
#   2. New-pattern discovery (--discover) — scan terminal-output snippets logged
#      around "needs_user" ledger entries for recurring phrases that aren't yet
#      a pattern, and propose them as pattern candidates in proposals.jsonl for
#      the user to confirm.

PATTERN_FEEDBACK = BIN / "tools" / "pattern-feedback.py"
PATTERN_BANK_PATH = HOME / ".assistant" / "pattern_bank.json"
FIRED_PATTERNS_LOG = HOME / ".assistant" / "cmux-fired-patterns.jsonl"

# A correction is about notification behavior when it names pinging/notifying.
_NOTIFICATION_CORRECTION_RE = re.compile(
    r"\b(ping\w*|notif\w*|alert\w*|messag\w*|telegram)\b", re.I)
# …and carries a negative/stop sentiment (so neutral mentions of "ping" don't fire).
_NOTIFICATION_NEGATIVE_RE = re.compile(
    r"\b(stop|don'?t|do not|wasn'?t worth|not worth|too many|quit|no more|"
    r"useless|noise|noisy|annoying|spam\w*)\b", re.I)


def load_pattern_ids(bank_path: Path = PATTERN_BANK_PATH) -> list[dict[str, Any]]:
    """The patterns currently in the bank, or [] if it's missing/corrupt."""
    try:
        data = json.loads(bank_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    pats = data.get("patterns", [])
    return [p for p in pats if isinstance(p, dict) and p.get("id")]


def is_notification_correction(text: str) -> bool:
    """True if a user correction is specifically about notification/ping noise."""
    return bool(_NOTIFICATION_CORRECTION_RE.search(text or "")
                and _NOTIFICATION_NEGATIVE_RE.search(text or ""))


def match_pattern_for_correction(text: str,
                                 patterns: list[dict[str, Any]]) -> str | None:
    """Best-effort map a noise correction to a pattern id: the pattern whose id
    or signal words appear in the correction text. Returns None if no pattern is
    clearly implicated (then we don't guess — a wrong mute is worse than none)."""
    low = (text or "").lower()
    for p in patterns:
        pid = (p.get("id") or "").lower()
        if not pid:
            continue
        # id is kebab-case; match on any of its words appearing in the text.
        words = [w for w in re.split(r"[-_]", pid) if len(w) >= 3]
        if words and all(w in low for w in words):
            return p.get("id")
    for p in patterns:
        # Fall back to a single distinctive word from the id.
        for w in re.split(r"[-_]", (p.get("id") or "").lower()):
            if len(w) >= 5 and w in low:
                return p.get("id")
    return None


def send_noise_feedback(pattern_id: str,
                        feedback_cli: Path = PATTERN_FEEDBACK,
                        runner: Callable[[list[str]], tuple[int, str, str]] | None = None
                        ) -> bool:
    """Invoke pattern-feedback.py with feedback=noise for pattern_id."""
    runner = runner or _run
    rc, _out, _err = runner([
        sys.executable, str(feedback_cli),
        "--pattern-id", pattern_id, "--feedback", "noise",
    ])
    return rc == 0


def apply_notification_feedback(candidates: list[dict[str, Any]], *,
                                dry_run: bool = False,
                                bank_path: Path = PATTERN_BANK_PATH,
                                feedback_cli: Path = PATTERN_FEEDBACK,
                                runner: Callable[[list[str]], tuple[int, str, str]] | None = None
                                ) -> list[dict[str, Any]]:
    """For every transcript correction that's about notification noise, downgrade
    the implicated cmux-watcher pattern via pattern-feedback.py. Returns the list
    of {pattern_id, candidate} pairs acted on. Never raises."""
    patterns = load_pattern_ids(bank_path)
    if not patterns:
        return []
    acted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for cand in candidates:
        if cand.get("type") != "transcript" or cand.get("signal") != "correction":
            continue
        text = cand.get("user_signal", "")
        if not is_notification_correction(text):
            continue
        pid = match_pattern_for_correction(text, patterns)
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        if dry_run:
            audit(f"[dry-run] would mark pattern {pid!r} noise from correction "
                  f"{text[:80]!r}")
            acted.append({"pattern_id": pid, "candidate": cand, "dry_run": True})
            continue
        ok = send_noise_feedback(pid, feedback_cli, runner)
        audit(f"notification-feedback: pattern={pid!r} noise ok={ok} "
              f"from correction {text[:80]!r}")
        acted.append({"pattern_id": pid, "candidate": cand, "ok": ok})
    return acted


def read_fired_snippets(path: Path = FIRED_PATTERNS_LOG,
                        n: int = 500) -> list[dict[str, Any]]:
    """Last n rows of the cmux-watcher fired-patterns log (best effort)."""
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


# A candidate phrase from a needs_user terminal snippet: a short, content-bearing
# line. We strip box-drawing, prompts, and timestamps, then key on the stem.
_SNIPPET_NOISE_RE = re.compile(r"[│─╭╮╰╯▔▕>·•⏵]+")


def discover_patterns(*, ledger_path: Path = LEDGER_PATH,
                      bank_path: Path = PATTERN_BANK_PATH,
                      proposals_path: Path = PROPOSALS_PATH,
                      min_occurrences: int = 2,
                      now: int | None = None) -> list[dict[str, Any]]:
    """Scan terminal-output snippets that preceded needs_user ledger entries for
    recurring phrases NOT yet covered by a bank pattern, and return proposal
    candidates. Pure of side effects beyond reading; the caller writes proposals.

    Signal source: the cmux-watcher logs each fired pattern + ws_ref to
    cmux-fired-patterns.jsonl, and pulse.py logs needs_user emit-card entries to
    the action ledger with the workspace's screen evidence. We mine the ledger's
    needs_user evidence for phrases that recur but match no existing pattern —
    those are the gaps the bank should grow to cover."""
    now = now if now is not None else now_epoch()
    existing = load_pattern_ids(bank_path)
    compiled: list[re.Pattern] = []
    for p in existing:
        try:
            compiled.append(re.compile(p.get("regex", ""), re.I))
        except re.error:
            continue

    entries = read_ledger_tail(ledger_path, n=1000)
    # Phrases tied to needs_user / emit-card outcomes — that's the signal that a
    # human got pulled in, i.e. exactly what a new pattern should catch earlier.
    phrase_counts: dict[str, int] = defaultdict(int)
    phrase_repr: dict[str, str] = {}
    for e in entries:
        kind = e.get("kind") or ""
        if kind not in ("emit-card", "needs_user"):
            continue
        evidence = (e.get("evidence") or "").strip()
        if not evidence:
            continue
        for raw_line in evidence.splitlines():
            line = _SNIPPET_NOISE_RE.sub(" ", raw_line).strip()
            if len(line) < 12 or len(line) > 160:
                continue
            # Already covered by a bank pattern? Then it's not a gap.
            if any(rx.search(line) for rx in compiled):
                continue
            stem = signal_stem(line, words=8)
            if not stem:
                continue
            phrase_counts[stem] += 1
            phrase_repr.setdefault(stem, line)

    # Stems already proposed as a pattern (don't re-propose every run).
    proposed_stems = _pending_pattern_stems(proposals_path)

    candidates: list[dict[str, Any]] = []
    for stem, count in sorted(phrase_counts.items(), key=lambda kv: kv[1], reverse=True):
        if count < min_occurrences:
            continue
        if _norm(stem) in proposed_stems:
            continue
        candidates.append({
            "type": "pattern",
            "stem": stem,
            "sample": phrase_repr[stem],
            "count": count,
        })
    return candidates


def _pending_pattern_stems(path: Path = PROPOSALS_PATH) -> set[str]:
    """Stems already represented by a pending/confirmed PATTERN proposal."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return set()
    out: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "pattern":
            continue
        if obj.get("status") not in ("pending", "confirmed"):
            continue
        stem = obj.get("pattern_stem") or ""
        if stem:
            out.add(_norm(stem))
    return out


def write_pattern_proposal(candidate: dict[str, Any],
                           path: Path = PROPOSALS_PATH) -> str:
    """Append one pending PATTERN proposal (distinct from lesson proposals so
    the confirm flow can tell them apart). Returns its id."""
    ts = utc_iso_us()
    # Derive a kebab id + a literal-substring regex from the representative line.
    sample = candidate.get("sample", "")
    pid = "-".join(re.findall(r"[a-z0-9]+", sample.lower())[:4]) or "discovered"
    entry = {
        "ts": ts,
        "id": ts,
        "type": "pattern",
        "proposed_pattern": {
            "id": pid,
            "regex": re.escape(sample.strip())[:200],
            "signal": "needs_input",
            "priority": "medium",
        },
        "pattern_stem": candidate["stem"],
        "sample": sample,
        "count": candidate["count"],
        "source": "extractor-discover",
        "status": "pending",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return ts


def run_discovery(*, dry_run: bool = False,
                  ledger_path: Path = LEDGER_PATH,
                  bank_path: Path = PATTERN_BANK_PATH,
                  proposals_path: Path = PROPOSALS_PATH,
                  tg_send: Path = TG_SEND,
                  now: int | None = None) -> dict[str, Any]:
    """Discover + propose new patterns. Returns a summary dict."""
    cands = discover_patterns(ledger_path=ledger_path, bank_path=bank_path,
                              proposals_path=proposals_path, now=now)
    proposed: list[dict[str, Any]] = []
    for c in cands:
        if dry_run:
            audit(f"[dry-run] would propose pattern stem={c['stem']!r} "
                  f"count={c['count']} sample={c['sample'][:80]!r}")
            proposed.append({"dry_run": True, "candidate": c})
            continue
        pid = write_pattern_proposal(c, proposals_path)
        pinged = ping_user(f"new watcher pattern: {c['sample'][:60]}", tg_send)
        audit(f"proposed pattern id={pid} stem={c['stem']!r} count={c['count']} "
              f"pinged={pinged}")
        proposed.append({"id": pid, "candidate": c, "pinged": pinged})
    return {"n_candidates": len(cands), "n_proposed": len(proposed),
            "proposed": proposed, "dry_run": dry_run}


# ─── orchestration ───────────────────────────────────────────────────────────

def extract(*, dry_run: bool = False,
            ledger_only: bool = False,
            llm: Callable[[str], str] | None = None,
            ledger_path: Path = LEDGER_PATH,
            proposals_path: Path = PROPOSALS_PATH,
            curator: Path = CURATOR,
            tg_send: Path = TG_SEND,
            scanner_factory: Callable[[list[str]], Any] | None = None,
            now: int | None = None) -> dict[str, Any]:
    """Run one extraction pass. Returns a summary dict.

    Pass 1 mines the action ledger; Pass 2 (unless ledger_only) mines Claude
    session transcripts. Both feed the same dedup → draft → propose pipeline.
    Designed for both pulse.py (defaults) and tests (inject llm + paths + now +
    scanner_factory).
    """
    now = now if now is not None else now_epoch()
    entries = read_ledger_tail(ledger_path)
    candidates = find_patterns(entries, now=now)

    triggers = existing_triggers(curator)
    pending = pending_proposal_stems(proposals_path)

    # Pass 2: transcript signals. The scanner dedups against existing lessons
    # itself (fuzzy first-10-words) and is bounded; ledger candidates lead so
    # the strongest recurring actions still propose first.
    n_transcripts = 0
    if not ledger_only:
        if scanner_factory is not None:
            scanner = scanner_factory(triggers)
        else:
            scanner = TranscriptScanner(existing=triggers, now=float(now))
        try:
            transcript_cands = scanner.scan()
            n_transcripts = len(transcript_cands)
            candidates = candidates + transcript_cands
        except Exception as e:  # noqa: BLE001 — a scan failure must not abort Pass 1
            audit(f"transcript scan failed: {e}")

    # Notification noise feedback: any transcript correction about pinging gets
    # routed to pattern-feedback.py so a noisy cmux-watcher pattern self-mutes.
    # Best-effort — never aborts extraction.
    n_feedback = 0
    try:
        acted = apply_notification_feedback(candidates, dry_run=dry_run)
        n_feedback = len(acted)
    except Exception as e:  # noqa: BLE001
        audit(f"notification feedback failed: {e}")

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
        "n_transcript_candidates": n_transcripts,
        "n_notification_feedback": n_feedback,
        "n_proposed": len(proposed),
        "n_skipped": len(skipped),
        "proposed": proposed,
        "skipped": skipped,
        "dry_run": dry_run,
        "ledger_only": ledger_only,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Detect recurring patterns in the action ledger AND Claude "
                    "session transcripts, and draft lesson proposals the user "
                    "can confirm.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print proposals without writing them or pinging.")
    ap.add_argument("--ledger-only", action="store_true",
                    help="Skip the (slower) transcript scan; mine only the "
                         "action ledger. Used for fast pulse-driven runs.")
    ap.add_argument("--discover", action="store_true",
                    help="Discover NEW cmux-watcher patterns: scan terminal "
                         "snippets that preceded needs_user ledger entries for "
                         "recurring phrases no current pattern catches, and "
                         "propose them in proposals.jsonl for the user to confirm.")
    args = ap.parse_args(argv)

    if args.discover:
        dres = run_discovery(dry_run=args.dry_run)
        print(json.dumps({
            "mode": "discover",
            "n_candidates": dres["n_candidates"],
            "n_proposed": dres["n_proposed"],
            "proposed": [
                {"stem": p["candidate"]["stem"],
                 "sample": p["candidate"]["sample"],
                 "count": p["candidate"]["count"]}
                for p in dres["proposed"]
            ],
        }, indent=2, ensure_ascii=False))
        return 0

    result = extract(dry_run=args.dry_run, ledger_only=args.ledger_only)

    if args.dry_run:
        # Human-readable summary to stdout in dry-run.
        print(json.dumps({
            "n_entries": result["n_entries"],
            "n_candidates": result["n_candidates"],
            "n_transcript_candidates": result["n_transcript_candidates"],
            "would_propose": [
                {"trigger": p["draft"]["trigger"], "rule": p["draft"]["rule"],
                 "target": p["draft"]["target"], "scope": p["draft"]["scope"],
                 "pattern": {"kind": p["candidate"]["kind"],
                             "count": p["candidate"]["count"],
                             "stem": p["candidate"]["stem"],
                             "signal": p["candidate"].get("signal"),
                             "source_file": p["candidate"].get("source_file")}}
                for p in result["proposed"]
            ],
            "skipped": [{"reason": s["reason"], "kind": s["kind"],
                         "stem": s["stem"], "count": s["count"]}
                        for s in result["skipped"]],
        }, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({k: result[k] for k in
                          ("n_entries", "n_candidates", "n_transcript_candidates",
                           "n_proposed", "n_skipped")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
