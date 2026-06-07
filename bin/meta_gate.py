#!/usr/bin/env python3
"""archffp meta-gate — TDD discipline for FFP rule + skill changes.

`/archffp --meta` is the forced mode for adding or updating a rule under
firefly-platform's `.claude/rules/**` or a skill under `.claude/skills/**`. Such a
change carries no production code, so the normal fix/feature pipeline has nothing
to reproduce, fix, or build — but a careless edit can still introduce a duplicate,
a conflict, or a behavior the agents silently stop honoring. The meta-gate is the
four-gate discipline that closes that hole, applying ordinary TDD to rules/skills.

The four gates (orchestrator-driven; this module owns the pure, testable pieces):

  Gate 1 — Dedup/conflict (PRE-work, BLOCK before touching anything).
           Read every existing lesson store + the existing skill list, and ask in
           ONE LLM call: does the PROPOSED change duplicate or conflict with
           anything already present? Yes → BLOCK naming the duplicate/conflict.
  Gate 2 — Do the real work. Apply the rule/skill add/update. No gate here.
  Gate 3 — Regression (POST-work). Run the existing archffp evals; any eval that
           now fails → BLOCK ("change broke eval <name>; fix it or update the eval
           intentionally"). Orchestrator-run (the live eval harness), like every
           other archffp gate — not this module.
  Gate 4 — Coverage / TDD (POST-work). Is the change covered by an existing eval?
           Search the fixtures. If covered → pass. If NOT → write a new fixture,
           prove RED (fails without the change) → GREEN (passes with it) → RED
           again on revert, then commit the fixture alongside the change.

This module owns Gate 1 (`dedup_gate`, injected LLM) and Gate 4's coverage search
(`coverage_search`), plus `detect`. The LLM call is injected so the unit tests
drive the block logic without ever touching Bedrock — same pattern
test_lesson_extractor.py uses to test extract() with an injected llm.

SCOPE. firefly-platform rule/skill updates only (`.claude/rules/**`,
`.claude/skills/**`). archffp's OWN rules/skills and `src/ffp-context.md` are out
of scope — a future `/archself` mode owns changes to archffp itself.

CLI:
  meta_gate.py detect   --paths a/b.md ...            # exit 0 if FFP rule/skill, 10 if not
  meta_gate.py detect   --repo ~/dev/firefly-platform [--base origin/main]
  meta_gate.py coverage --keywords k1 k2 --fixtures-dir <dir> [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ASSISTANT_DIR = Path(__file__).resolve().parent.parent
LESSON_EXTRACTOR = ASSISTANT_DIR / "bin" / "lesson-extractor.py"
CURATOR = ASSISTANT_DIR / "bin" / "assistant-curator.py"
# architect-ffp's eval grid — where Gate 4 looks for / writes a covering fixture.
ARCHFFP_FIXTURES = Path.home() / "dev" / "architect-ffp" / "evals" / "fixtures"


# ─── meta-change detection (FFP rules / skills only) ────────────────────────

_META_SEGMENTS = (".claude/rules/", ".claude/skills/")


def is_meta_path(path: str) -> bool:
    """True if a single changed path is an FFP rule or skill file."""
    p = str(path).replace("\\", "/")
    return any(seg in p for seg in _META_SEGMENTS)


def meta_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if is_meta_path(p)]


def is_meta_change(paths: list[str]) -> bool:
    return any(is_meta_path(p) for p in paths)


# ─── Gate 1 — dedup / conflict (pre-work) ───────────────────────────────────

def gather_existing_corpus(*, curator: Path = CURATOR,
                           ffp_skills_dir: Path | None = None) -> dict[str, Any]:
    """Best-effort gather of everything Gate 1 must dedup against: the lessons in
    all five stores (via the curator's `list`) plus the existing FFP skill names.

    Never raises — a store that can't be listed is simply omitted. Returns
    {"lessons": [{"store","slug","trigger"}], "skills": ["name", ...]}. Tests
    bypass this and pass the corpus straight to `dedup_gate`.
    """
    lessons: list[dict[str, str]] = []
    for target in ("claude", "assistant", "ffp", "archffp", "assistant-repo"):
        try:
            r = subprocess.run([sys.executable, str(curator), "list", "--target", target],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                continue
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 1)
                lessons.append({"store": target,
                                "slug": parts[0].strip() if parts else "",
                                "trigger": parts[1].strip() if len(parts) > 1 else line})
        except (OSError, subprocess.SubprocessError):
            continue

    skills: list[str] = []
    sk = ffp_skills_dir or (Path.home() / "dev" / "firefly-platform" / ".claude" / "skills")
    try:
        if sk.exists():
            skills = sorted(d.name for d in sk.iterdir() if d.is_dir())
    except OSError:
        pass
    return {"lessons": lessons, "skills": skills}


def _dedup_prompt(proposed: str, corpus: dict[str, Any]) -> str:
    lesson_lines = "\n".join(
        f"  [{l.get('store','?')}] {l.get('slug','?')}: {l.get('trigger','')}"
        for l in corpus.get("lessons", []))
    skill_lines = ", ".join(corpus.get("skills", [])) or "(none)"
    return (
        "You are gating a proposed change to a Claude Code rule or skill.\n\n"
        "PROPOSED CHANGE:\n"
        f"{proposed.strip()}\n\n"
        "EXISTING LESSONS (all stores):\n"
        f"{lesson_lines or '  (none)'}\n\n"
        "EXISTING SKILLS:\n"
        f"  {skill_lines}\n\n"
        "Does the PROPOSED change DUPLICATE (same intent as an existing lesson/skill,"
        " even if worded differently) or CONFLICT (contradicts an existing one)"
        " anything already present?\n"
        "Return JSON: {\"duplicate\": bool, \"conflict\": bool, "
        "\"matches\": [{\"kind\": \"duplicate\"|\"conflict\", \"ref\": \"store/slug or skill\", "
        "\"why\": \"one sentence\"}]}\n"
        "Be conservative — only flag a clear duplicate or a real contradiction."
    )


def dedup_gate(proposed: str, corpus: dict[str, Any] | None = None, *,
               run_llm: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Gate 1. Ask the injected LLM whether `proposed` duplicates/conflicts with
    anything in `corpus`. BLOCK on either. `run_llm(prompt) -> json-string`.
    """
    if corpus is None:
        corpus = gather_existing_corpus()
    if run_llm is None:
        run_llm = _claude_oneshot
    prompt = _dedup_prompt(proposed, corpus)
    try:
        data = json.loads(run_llm(prompt))
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # A gate that can't run must not silently pass — surface, don't block-by-default
        # here (the orchestrator decides); report the tooling failure.
        return {"gate": 1, "ok": True, "block": False, "error": f"dedup LLM call unparseable: {e}",
                "message": "Gate 1 (dedup): could not run the dedup check — surface to operator."}
    matches = data.get("matches", []) or []
    if data.get("duplicate") or data.get("conflict") or matches:
        lines = [f"  - {m.get('kind','match')} of {m.get('ref','?')}: {m.get('why','')}" for m in matches]
        body = "\n".join(lines) if lines else "  - (LLM flagged a duplicate/conflict)"
        return {"gate": 1, "ok": False, "block": True, "matches": matches,
                "message": ("Meta-gate Gate 1 (dedup): the proposed rule/skill duplicates or "
                            "conflicts with something already present:\n" + body
                            + "\nMerge into / reword against the existing one, or revise the change.")}
    return {"gate": 1, "ok": True, "block": False, "matches": [],
            "message": "Gate 1 (dedup): no duplicate or conflict found."}


def _claude_oneshot(prompt: str) -> str:
    """Default Gate-1 LLM — reuse lesson-extractor's one-shot helper so the meta-gate
    and the lesson auditor make the call the same way. Only reached in a live run."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lesson_extractor_mod", LESSON_EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._claude_oneshot(prompt)


# ─── Gate 4 — coverage search (post-work, TDD) ──────────────────────────────

_STOPWORDS = frozenset("""
about above after again against also when then must always never with within would your
into just keep more most must need only over same some such that this when will rule
rules lesson lessons skill skills change should from they them have here there which
""".split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) >= 4 and t not in _STOPWORDS}


def coverage_search(keywords: list[str] | str, *, fixtures_dir: Path = ARCHFFP_FIXTURES) -> dict[str, Any]:
    """Gate 4 helper. Search the archffp eval fixtures for any cell whose input/
    rubric mentions the change. Returns {"covered": bool, "matches": [...]}.

    `keywords` may be a list or a free-text description; significant tokens are
    extracted and matched against each fixture's searchable text. A miss is the
    TDD signal to WRITE a new fixture (RED→GREEN→RED-on-revert) — this function
    only answers "is there already a fixture that mentions this?".
    """
    kw = keywords if isinstance(keywords, list) else [keywords]
    want = set()
    for k in kw:
        want |= _tokens(k)
    matches: list[dict[str, Any]] = []
    fixtures_dir = Path(fixtures_dir)
    if want and fixtures_dir.exists():
        for cell_file in sorted(fixtures_dir.rglob("*")):
            if not cell_file.is_file():
                continue
            if cell_file.suffix not in (".md", ".json", ".txt"):
                continue
            try:
                toks = _tokens(cell_file.read_text(errors="replace")[:200_000])
            except OSError:
                continue
            shared = want & toks
            if shared:
                rel = cell_file.relative_to(fixtures_dir)
                matches.append({"fixture": str(rel), "shared": sorted(shared)})
    return {"covered": bool(matches), "matches": matches,
            "message": ("Gate 4 (coverage): an existing fixture mentions this change."
                        if matches else
                        "Gate 4 (coverage): NO fixture covers this change — write one "
                        "(prove RED→GREEN→RED-on-revert) and commit it alongside.")}


# ─── git helpers (CLI only — never imported by tests) ───────────────────────

def _git_changed_paths(repo: Path, base: str) -> list[str]:
    paths: set[str] = set()
    for args in (["diff", "--name-only", f"{base}...HEAD"],
                 ["diff", "--name-only"],
                 ["diff", "--name-only", "--staged"]):
        try:
            proc = subprocess.run(["git", "-C", str(repo), *args],
                                  capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                paths.update(l.strip() for l in proc.stdout.splitlines() if l.strip())
        except (OSError, subprocess.SubprocessError):
            continue
    return sorted(paths)


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="Exit 0 if paths are an FFP rule/skill change, 10 if not.")
    d.add_argument("--paths", nargs="*", default=[])
    d.add_argument("--repo", default=None)
    d.add_argument("--base", default="origin/main")

    cov = sub.add_parser("coverage", help="Gate 4 — search archffp fixtures for the change.")
    cov.add_argument("--keywords", nargs="+", required=True)
    cov.add_argument("--fixtures-dir", default=str(ARCHFFP_FIXTURES))
    cov.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "detect":
        paths = list(args.paths)
        if args.repo:
            paths.extend(_git_changed_paths(Path(args.repo), args.base))
        hit = meta_paths(paths)
        if hit:
            print("META_GATE: FFP rule/skill change detected:")
            for p in hit:
                print(f"  {p}")
            return 0
        print("META_GATE: not a meta-change (no .claude/rules/ or .claude/skills/ files touched)")
        return 10

    if args.cmd == "coverage":
        res = coverage_search(list(args.keywords), fixtures_dir=Path(args.fixtures_dir))
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(res["message"])
            for m in res["matches"]:
                print(f"  {m['fixture']}  (shared: {', '.join(m['shared'])})")
        return 0 if res["covered"] else 10

    return 2


if __name__ == "__main__":
    sys.exit(main())
