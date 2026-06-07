#!/usr/bin/env python3
"""archffp meta-gate — lesson/rule audit for FFP rule + skill changes.

archffp ships code to firefly-platform behind gates (E2E, CI). A diff that only
touches firefly-platform's `.claude/rules/` or `.claude/skills/` carries no
production code, so the normal pipeline has nothing to reproduce, fix, or build —
yet a careless rule edit can still introduce a near-duplicate or a directly
conflicting lesson. This gate is the one check that shape needs: a duplication /
conflict audit. archffp's Step 0.5 invokes it; the result drives a `[META]` PR
that still goes through the same CI + review flow as any other archffp PR.

ONE gate, ONE block condition:

  Audit — `lesson-extractor.py --audit --dry-run` reads every lesson store and
          flags near-duplicates and verbose/conflicting lessons. Any finding is
          a BLOCK; zero findings passes.

SCOPE. This mode is for **firefly-platform** rule/skill updates only
(`.claude/rules/**`, `.claude/skills/**`). architect-ffp's own rules/skills are
out of scope — a future `/archself` mode owns those.

DESIGN — why the audit runner is injected. The real audit makes a one-shot LLM
call (`lesson-extractor.py` shells out to `claude`). It is injected as a callable
(`run_audit`) so the unit tests drive the gate's block logic without ever touching
Bedrock — the same pattern test_lesson_extractor.py uses to test extract() with an
injected llm. The default runner shells out for real and is only reached in a live
pipeline run.

CLI:
  meta_gate.py detect --paths a/b.md c/d.md   # exit 0 if FFP rule/skill change, 10 if not
  meta_gate.py detect --repo ~/dev/firefly-platform [--base origin/main]
  meta_gate.py check  [--repo ~/dev/firefly-platform] [--paths ...] [--json]
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


# ─── meta-change detection (FFP rules / skills only) ────────────────────────
#
# A diff is "meta" when it touches firefly-platform's `.claude/rules/**` or
# `.claude/skills/**`. We match on the `.claude/rules/` and `.claude/skills/`
# path segments so the check works whether git reports paths repo-relative
# (`.claude/rules/ai.md`) or with a leading repo dir (`firefly-platform/.claude/
# skills/foo/SKILL.md`). architect-ffp's own `skills/` and `src/ffp-context.md`
# are deliberately NOT matched — those belong to the future /archself mode.

_META_SEGMENTS = (".claude/rules/", ".claude/skills/")


def is_meta_path(path: str) -> bool:
    """True if a single changed path is an FFP rule or skill file."""
    p = str(path).replace("\\", "/")
    return any(seg in p for seg in _META_SEGMENTS)


def meta_paths(paths: list[str]) -> list[str]:
    """Subset of `paths` that are FFP rule/skill files."""
    return [p for p in paths if is_meta_path(p)]


def is_meta_change(paths: list[str]) -> bool:
    """True if any changed path triggers the meta-gate."""
    return any(is_meta_path(p) for p in paths)


# ─── the one gate — lesson audit ────────────────────────────────────────────

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
    # n_findings as the count of issues (near-dupes + verbose/conflicting).
    return {"n_findings": int(data.get("n_findings", 0)), "raw": data}


def _fmt_finding(f: dict[str, Any]) -> str:
    action = f.get("action", "issue")
    slugs = ", ".join(f.get("slugs", [])) or "?"
    reason = f.get("reason", "")
    return f"{action} [{slugs}] — {reason}".strip(" —")


def audit_gate(*, run_audit: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the lesson quality audit; BLOCK on any near-duplicate or conflict.

    `run_audit()` returns {"n_findings": int, "findings": [{action,slugs,reason}]}.
    """
    run_audit = run_audit or _default_audit_runner
    result = run_audit()
    n = int(result.get("n_findings", 0) or 0)
    if n > 0:
        findings = result.get("findings", []) or []
        detail = "; ".join(_fmt_finding(f) for f in findings) if findings else f"{n} finding(s)"
        return {"ok": False, "block": True, "n_findings": n,
                "message": f"Lesson audit found issues: {detail}. Resolve before shipping."}
    return {"ok": True, "block": False, "n_findings": 0,
            "message": "meta-gate: lesson audit clean — no near-duplicate or conflicting lessons."}


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

    d = sub.add_parser("detect", help="Exit 0 if the paths are an FFP rule/skill change, 10 if not.")
    d.add_argument("--paths", nargs="*", default=[])
    d.add_argument("--repo", default=None, help="Compute changed paths from this repo instead of --paths.")
    d.add_argument("--base", default="origin/main")

    c = sub.add_parser("check", help="Run the meta-gate (audit) if a meta-change is present.")
    c.add_argument("--repo", default=None, help="Repo whose diff to inspect.")
    c.add_argument("--base", default="origin/main")
    c.add_argument("--paths", nargs="*", default=[], help="Explicit changed paths (bypass git).")
    c.add_argument("--json", action="store_true")

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
        print("META_GATE: SKIP (no .claude/rules/ or .claude/skills/ files touched)")
        return 10

    # check
    changed = list(args.paths)
    if args.repo:
        changed.extend(_git_changed_paths(Path(args.repo), args.base))
    hit = meta_paths(changed)
    if not hit:
        print("META_GATE: SKIP (no .claude/rules/ or .claude/skills/ files touched) — continuing to Step 1.")
        return 0

    result = audit_gate()
    if args.json:
        print(json.dumps({"triggered_by": hit, **result}, indent=2, ensure_ascii=False))
    else:
        print(f"META_GATE: triggered by {len(hit)} FFP rule/skill file(s).")
        tag = "BLOCK" if result["block"] else "PASS"
        print(f"  [{tag}] {result['message']}")
    return 20 if result["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
