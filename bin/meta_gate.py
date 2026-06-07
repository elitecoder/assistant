#!/usr/bin/env python3
"""archffp meta-gate — eval coverage for rules / skills / prompt changes.

archffp ships code to firefly-platform behind gates (evals, E2E, CI). Changes to
`.claude/rules/`, `skills/`, and prompts bypassed every gate — a bad lesson or a
skill edit that flips Observer verdict behavior would never be caught. This module
is the missing gate. archffp's Step 0.5 invokes it; the orchestrator never
re-implements the checks.

Three checks run in sequence; any BLOCK halts the pipeline:

  Gate 1 — Regression: the Observer eval suite (evals/observer/run.py) must hold
           every fixture's expected verdict. A flip is a BLOCK.
  Gate 2 — Coverage: every rule/lesson trigger added or modified must have at
           least one Observer fixture that plausibly exercises it. Zero coverage
           is a BLOCK and a skeleton fixture is scaffolded for the operator.
  Gate 3 — Audit: lesson-extractor.py's quality pass must find no near-duplicate
           or conflicting lessons. Any finding is a BLOCK.

DESIGN — why the expensive work is injected. Gate 1 shells out to the Observer
suite (one `claude` call per fixture, real Bedrock spend) and Gate 3 makes a
one-shot LLM call. Both are injected as callables (`run_eval`, `run_audit`) so the
unit tests drive the gate logic without ever touching Bedrock — the same pattern
test_lesson_extractor.py uses to test extract() with an injected llm. The default
runners shell out for real and are only reached in a live pipeline run.

GATE 2 IS A SCREEN, NOT A PROOF. The coverage check is a keyword overlap between a
rule's trigger text and each fixture's searchable text (ctx title + notes/README +
transcript). It can show that *no* fixture plausibly relates to a new rule (the
case worth blocking on); it cannot prove a matched fixture actually exercises the
rule. Matched tokens are reported so a human can confirm.

CLI:
  meta_gate.py check --repo ~/dev/firefly-platform [--base origin/main] [--skip-regression] [--json]
  meta_gate.py detect --paths a/b.md c/d.md          # exit 0 if meta-change, 10 if not
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ASSISTANT_DIR = Path(__file__).resolve().parent.parent
OBSERVER_EVAL_RUN = ASSISTANT_DIR / "evals" / "observer" / "run.py"
OBSERVER_FIXTURES = ASSISTANT_DIR / "evals" / "observer" / "fixtures"
LESSON_EXTRACTOR = ASSISTANT_DIR / "bin" / "lesson-extractor.py"

# Paths whose change makes a diff "meta" — bypasses the normal code gates today.
#   firefly-platform/.claude/rules/**   (rules incl. ffp-lessons.md)
#   architect-ffp/skills/**             (skill bodies)
#   architect-ffp/src/ffp-context.md    (the shared agent prelude)
#   any prompts/ directory
_META_SUBSTRINGS = ("/.claude/rules/", "/skills/", "/prompts/")
_META_PREFIXES = (".claude/rules/", "skills/", "prompts/")
_META_SUFFIXES = ("ffp-context.md",)

# Tokens too common to carry signal in a keyword overlap. Generic English plus a
# few domain words that appear in nearly every fixture/rule and would make the
# coverage screen match everything.
STOPWORDS = frozenset("""
about above after again against also another back because been before being below
between both came cannot come could does done down each else even ever every from
ghost gives goes gone half hand have here here's into just keep kept know less like
made make many more most much must need needs never next none null only onto open
over part past plus same says seen self some soon stop such sure take taken than
that's them then there these they this those took true turn under until upon used
uses very want well went were what's when where which while will with within would
your archffp claude observer workspace session pipeline rule rules lesson lessons
skill skills prompt prompts file files change changes when then must always never
""".split())


# ─── meta-change detection ──────────────────────────────────────────────────

def is_meta_path(path: str) -> bool:
    """True if a single changed path is rules / skills / prompts / ffp-context."""
    p = str(path).replace("\\", "/")
    if any(p.startswith(pre) for pre in _META_PREFIXES):
        return True
    if any(sub in p for sub in _META_SUBSTRINGS):
        return True
    if any(p.endswith(suf) for suf in _META_SUFFIXES):
        return True
    return False


def meta_paths(paths: list[str]) -> list[str]:
    """Subset of `paths` that are meta files."""
    return [p for p in paths if is_meta_path(p)]


def is_meta_change(paths: list[str]) -> bool:
    """True if any changed path triggers the meta-gate."""
    return any(is_meta_path(p) for p in paths)


# ─── trigger extraction ─────────────────────────────────────────────────────

def extract_rule_triggers(text: str) -> list[str]:
    """Pull the trigger text out of a rule/lesson markdown body.

    Lesson stores (CLAUDE.md, ffp-lessons.md, archffp-lessons.md, the Observer
    prompt) write each rule as a bold-only line — `**About to run X**` — followed
    by the rule body. Those bold headers are the triggers. Falls back to `##`/`###`
    headers when a file uses no bold triggers. Boilerplate headers are dropped.
    """
    triggers: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        m = re.fullmatch(r"\*\*(.+?)\*\*", s)
        if m:
            t = m.group(1).strip()
            if t and t.lower().rstrip(":") not in ("why", "how to apply"):
                triggers.append(t)
    if triggers:
        return triggers
    for line in text.splitlines():
        s = line.strip()
        m = re.fullmatch(r"#{2,4}\s+(.+)", s)
        if m:
            t = m.group(1).strip()
            if t.lower() not in ("lessons", "why", "how to apply", "rules"):
                triggers.append(t)
    return triggers


def _significant_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of length >= 4 that aren't stopwords."""
    return {tok for tok in re.findall(r"[a-z0-9]+", text.lower())
            if len(tok) >= 4 and tok not in STOPWORDS}


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("-")
    return s or "rule"


# ─── fixture corpus ─────────────────────────────────────────────────────────

_TRANSCRIPT_CAP = 1_000_000  # bytes — transcripts can be >1MB; cap the read


def fixture_searchable_text(fixture_dir: Path) -> str:
    """Concatenate the text a keyword screen should see for one fixture:
    ctx.json (title etc.), expected.json, any *.md (notes/README), and a capped
    head of transcript.jsonl. Lowercased."""
    parts: list[str] = []
    for name in ("ctx.json", "expected.json"):
        p = fixture_dir / name
        if p.exists():
            try:
                parts.append(p.read_text())
            except OSError:
                pass
    for md in sorted(fixture_dir.glob("*.md")):
        try:
            parts.append(md.read_text())
        except OSError:
            pass
    tr = fixture_dir / "transcript.jsonl"
    if tr.exists():
        try:
            with open(tr, "r", errors="replace") as fp:
                parts.append(fp.read(_TRANSCRIPT_CAP))
        except OSError:
            pass
    return "\n".join(parts).lower()


def _list_fixtures(fixtures_dir: Path) -> list[Path]:
    if not fixtures_dir.exists():
        return []
    return sorted(d for d in fixtures_dir.iterdir() if d.is_dir())


# ─── Gate 1 — regression ────────────────────────────────────────────────────

def _parse_run_py_failures(stdout: str) -> list[dict[str, str]]:
    """Parse run.py's `  FAIL <name>: <msg>` summary lines into flip dicts.

    run.py prints, e.g.:
      FAIL 06-question-to-user: verdict='active' expected 'needs_user'  (full: ...)
      FAIL 01-...: DANGEROUS: verdict='ready_for_merge' is FORBIDDEN ...
    """
    flips: list[dict[str, str]] = []
    for line in stdout.splitlines():
        m = re.match(r"\s*FAIL\s+(\S+):\s*(.*)", line)
        if not m:
            continue
        name, msg = m.group(1), m.group(2)
        vm = re.search(r"verdict=['\"]?(\w+)['\"]?\s+expected\s+['\"]?(\w+)['\"]?", msg)
        if vm:
            flips.append({"name": name, "got": vm.group(1), "expected": vm.group(2)})
            continue
        dm = re.search(r"verdict=['\"]?(\w+)['\"]?\s+is\s+FORBIDDEN", msg)
        if dm:
            flips.append({"name": name, "got": dm.group(1), "expected": "(not forbidden)"})
            continue
        flips.append({"name": name, "got": "?", "expected": "?", "detail": msg})
    return flips


def _default_eval_runner() -> dict[str, Any]:
    """Run the real Observer suite. One `claude` call per fixture (Bedrock spend);
    only reached in a live pipeline run — unit tests inject `run_eval`."""
    if not OBSERVER_EVAL_RUN.exists():
        return {"failures": [], "total": 0, "error": f"missing {OBSERVER_EVAL_RUN}"}
    proc = subprocess.run(
        [sys.executable, str(OBSERVER_EVAL_RUN)],
        capture_output=True, text=True,
    )
    failures = _parse_run_py_failures(proc.stdout)
    total = 0
    tm = re.search(r"===\s*(\d+)/(\d+)\s+passed\s*===", proc.stdout)
    if tm:
        total = int(tm.group(2))
    return {"failures": failures, "total": total, "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-1500:]}


def gate1_regression(*, run_eval: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the Observer regression suite; BLOCK on any verdict flip.

    `run_eval()` returns {"failures": [{"name","expected","got"}], "total": int}.
    """
    run_eval = run_eval or _default_eval_runner
    result = run_eval()
    failures = result.get("failures", []) or []
    if failures:
        lines = [f"  - fixture {f.get('name','?')}: expected {f.get('expected','?')}, "
                 f"got {f.get('got','?')}" for f in failures]
        message = (
            "Rule/skill change flips Observer verdict:\n"
            + "\n".join(lines)
            + "\nFix the rule or update the fixture intentionally."
        )
        return {"gate": 1, "ok": False, "block": True, "message": message, "failures": failures}
    total = result.get("total", "?")
    return {"gate": 1, "ok": True, "block": False, "failures": [],
            "message": f"Gate 1 (regression): all {total} Observer fixtures hold their expected verdict."}


# ─── Gate 2 — coverage ──────────────────────────────────────────────────────

def create_skeleton_fixture(fixtures_dir: Path, slug: str, trigger: str) -> Path:
    """Scaffold an Observer fixture for an uncovered rule. The expected verdict is
    deliberately `FILL_IN` so the suite stays RED until the operator fills it in."""
    d = Path(fixtures_dir) / f"{slug}-skeleton"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ctx.json").write_text(
        json.dumps({"ws_ref": "workspace:99", "title": trigger, "transcript_path": None},
                   indent=2) + "\n")
    (d / "expected.json").write_text(
        json.dumps({"verdict": "FILL_IN", "reason": "Add the expected verdict for this rule"},
                   indent=2) + "\n")
    (d / "transcript.jsonl").write_text("")
    (d / "README.md").write_text(
        f"Skeleton fixture for rule: {trigger}.\n"
        f"Fill in expected.json verdict before merging.\n")
    return d


def coverage_for_triggers(triggers: list[str], *, fixtures_dir: Path,
                          create_skeletons: bool = True) -> dict[str, Any]:
    """Core coverage check over an explicit trigger list (no git, no file IO on
    rule files). For each trigger, find fixtures whose searchable text shares a
    significant token. Uncovered triggers BLOCK and (optionally) get a skeleton."""
    fixtures = _list_fixtures(fixtures_dir)
    fixture_tokens = {f.name: _significant_tokens(fixture_searchable_text(f)) for f in fixtures}

    covered: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    skeletons: list[str] = []

    for trigger in triggers:
        trig_tokens = _significant_tokens(trigger)
        matches: list[dict[str, Any]] = []
        for name, toks in fixture_tokens.items():
            overlap = trig_tokens & toks
            if overlap:
                matches.append({"fixture": name, "shared": sorted(overlap)})
        if matches:
            covered.append({"trigger": trigger, "matches": matches})
        else:
            slug = slugify(trigger)
            entry: dict[str, Any] = {"trigger": trigger, "slug": slug}
            if create_skeletons:
                skel = create_skeleton_fixture(fixtures_dir, slug, trigger)
                entry["skeleton"] = str(skel)
                skeletons.append(str(skel))
            uncovered.append(entry)

    if uncovered:
        lines = []
        for u in uncovered:
            loc = u.get("skeleton")
            tail = f" Skeleton fixture created at {loc}." if loc else ""
            lines.append(f"  - rule '{u['slug']}' has no eval coverage.{tail}")
        message = (
            "New rule(s) have no Observer eval coverage:\n"
            + "\n".join(lines)
            + "\nFill in the expected verdict in each skeleton's expected.json, then re-run archffp."
        )
        return {"gate": 2, "ok": False, "block": True, "message": message,
                "uncovered": uncovered, "covered": covered, "skeletons": skeletons}
    return {"gate": 2, "ok": True, "block": False, "uncovered": [], "covered": covered,
            "skeletons": [],
            "message": f"Gate 2 (coverage): all {len(triggers)} new rule trigger(s) have a candidate fixture."}


def gate2_coverage(rule_files: list[str | Path], *, fixtures_dir: Path,
                   create_skeletons: bool = True) -> dict[str, Any]:
    """File-driven coverage check: read each changed rule file, extract its
    triggers, and screen them against the fixture corpus."""
    triggers: list[str] = []
    for rf in rule_files:
        p = Path(rf)
        if not p.exists():
            continue
        triggers.extend(extract_rule_triggers(p.read_text()))
    if not triggers:
        return {"gate": 2, "ok": True, "block": False, "uncovered": [], "covered": [],
                "skeletons": [], "message": "Gate 2 (coverage): no rule triggers found in the changed files."}
    return coverage_for_triggers(triggers, fixtures_dir=fixtures_dir,
                                 create_skeletons=create_skeletons)


# ─── Gate 3 — lesson audit ──────────────────────────────────────────────────

def _default_audit_runner() -> dict[str, Any]:
    """Run lesson-extractor.py's quality pass (--audit --dry-run). One LLM call;
    only reached in a live pipeline run — unit tests inject `run_audit`."""
    if not LESSON_EXTRACTOR.exists():
        return {"n_findings": 0, "error": f"missing {LESSON_EXTRACTOR}"}
    proc = subprocess.run(
        [sys.executable, str(LESSON_EXTRACTOR), "--audit", "--dry-run"],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(proc.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return {"n_findings": 0, "error": "could not parse audit output",
                "stdout_tail": proc.stdout[-500:]}
    # run_audit returns {"n_lessons","n_findings","n_proposed","dry_run"}; treat
    # n_findings as the count of issues (near-dupes + verbose).
    return {"n_findings": int(data.get("n_findings", 0)), "raw": data}


def _fmt_finding(f: dict[str, Any]) -> str:
    action = f.get("action", "issue")
    slugs = ", ".join(f.get("slugs", [])) or "?"
    reason = f.get("reason", "")
    return f"{action} [{slugs}] — {reason}".strip(" —")


def gate3_audit(*, run_audit: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the lesson quality audit; BLOCK on any near-duplicate or verbose finding.

    `run_audit()` returns {"n_findings": int, "findings": [{action,slugs,reason}]}.
    """
    run_audit = run_audit or _default_audit_runner
    result = run_audit()
    n = int(result.get("n_findings", 0) or 0)
    if n > 0:
        findings = result.get("findings", []) or []
        detail = "; ".join(_fmt_finding(f) for f in findings) if findings else f"{n} finding(s)"
        return {"gate": 3, "ok": False, "block": True, "n_findings": n,
                "message": f"Lesson audit found issues: {detail}. Resolve before shipping."}
    return {"gate": 3, "ok": True, "block": False, "n_findings": 0,
            "message": "Gate 3 (audit): no near-duplicate or conflicting lessons."}


# ─── orchestration ──────────────────────────────────────────────────────────

def run_all_gates(rule_files: list[str | Path], *, fixtures_dir: Path = OBSERVER_FIXTURES,
                  run_eval: Callable[[], dict[str, Any]] | None = None,
                  run_audit: Callable[[], dict[str, Any]] | None = None,
                  skip_regression: bool = False,
                  create_skeletons: bool = True) -> dict[str, Any]:
    """Run Gates 1→2→3 in sequence, stopping at the first BLOCK."""
    results: list[dict[str, Any]] = []

    if not skip_regression:
        g1 = gate1_regression(run_eval=run_eval)
        results.append(g1)
        if g1["block"]:
            return {"block": True, "stopped_at": 1, "results": results, "message": g1["message"]}

    g2 = gate2_coverage(rule_files, fixtures_dir=fixtures_dir, create_skeletons=create_skeletons)
    results.append(g2)
    if g2["block"]:
        return {"block": True, "stopped_at": 2, "results": results, "message": g2["message"]}

    g3 = gate3_audit(run_audit=run_audit)
    results.append(g3)
    if g3["block"]:
        return {"block": True, "stopped_at": 3, "results": results, "message": g3["message"]}

    return {"block": False, "stopped_at": None, "results": results,
            "message": "meta-gate: all three checks pass"}


# ─── git helpers (CLI only — never imported by tests) ───────────────────────

def _git_changed_paths(repo: Path, base: str) -> list[str]:
    """Union of committed-vs-base + staged + unstaged changed paths in `repo`."""
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

    d = sub.add_parser("detect", help="Exit 0 if the given paths are a meta-change, 10 if not.")
    d.add_argument("--paths", nargs="*", default=[])
    d.add_argument("--repo", default=None, help="Compute changed paths from this repo instead of --paths.")
    d.add_argument("--base", default="origin/main")

    c = sub.add_parser("check", help="Run all three meta-gates (only if a meta-change is present).")
    c.add_argument("--repo", action="append", default=[], help="Repo whose diff to inspect (repeatable).")
    c.add_argument("--base", default="origin/main")
    c.add_argument("--paths", nargs="*", default=[], help="Explicit changed paths (bypass git).")
    c.add_argument("--fixtures-dir", default=str(OBSERVER_FIXTURES))
    c.add_argument("--skip-regression", action="store_true",
                   help="Skip Gate 1 (the Bedrock-spending Observer suite).")
    c.add_argument("--no-skeletons", action="store_true",
                   help="Report uncovered rules without scaffolding skeleton fixtures.")
    c.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "detect":
        paths = list(args.paths)
        if args.repo:
            paths.extend(_git_changed_paths(Path(args.repo), args.base))
        hit = meta_paths(paths)
        if hit:
            print("META_GATE: meta-change detected:")
            for p in hit:
                print(f"  {p}")
            return 0
        print("META_GATE: SKIP (no rules/skills/prompts files touched)")
        return 10

    # check
    changed: list[str] = list(args.paths)
    for repo in args.repo:
        changed.extend(_git_changed_paths(Path(repo), args.base))
    hit = meta_paths(changed)
    if not hit:
        print("META_GATE: SKIP (no rules/skills/prompts files touched) — continuing to Step 1.")
        return 0

    rule_files = [p for p in hit if "rules/" in p.replace("\\", "/") or p.endswith(".md")]
    # Resolve rule-file paths against their repo roots when --repo was given.
    resolved: list[str] = []
    repos = [Path(r) for r in args.repo]
    for rf in rule_files:
        if Path(rf).exists():
            resolved.append(rf)
            continue
        for repo in repos:
            cand = repo / rf
            if cand.exists():
                resolved.append(str(cand))
                break

    result = run_all_gates(resolved, fixtures_dir=Path(args.fixtures_dir),
                           skip_regression=args.skip_regression,
                           create_skeletons=not args.no_skeletons)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"META_GATE: triggered by {len(hit)} meta file(s).")
        for r in result["results"]:
            tag = "BLOCK" if r["block"] else "PASS"
            print(f"  [{tag}] {r['message'].splitlines()[0]}")
        if result["block"]:
            print("\n" + result["message"])
        else:
            print("\nmeta-gate: all three checks pass")
    return 20 if result["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
