"""Tests for bin/meta_gate.py — the archffp meta-gate.

The meta-gate is ONE check: a lesson duplication/conflict audit that fires when an
archffp diff touches firefly-platform's `.claude/rules/` or `.claude/skills/`. The
real audit makes an LLM call, so it takes an INJECTED runner here — every test is
hermetic (no `claude` call, no network). Same pattern test_lesson_extractor.py uses
to test extract() with an injected llm.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, fname: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(REPO / "bin" / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mg = _load("meta_gate_mod", "meta_gate.py")


# ─── the one gate: lesson audit ──────────────────────────────────────────────

def test_audit_clean_passes():
    """Audit finds nothing → gate passes, no block."""
    res = mg.audit_gate(run_audit=lambda: {"n_findings": 0, "findings": []})
    assert res["ok"] is True
    assert res["block"] is False
    assert "clean" in res["message"]


def test_audit_duplicate_blocks():
    """Audit reports a near-duplicate → BLOCK with the finding text."""
    run_audit = lambda: {"n_findings": 1, "findings": [
        {"action": "merge", "slugs": ["commit-work", "always-commit"],
         "reason": "both say commit when a unit of work finishes"}]}
    res = mg.audit_gate(run_audit=run_audit)
    assert res["block"] is True
    assert res["ok"] is False
    assert "Lesson audit found issues" in res["message"]
    assert "commit-work" in res["message"]


def test_audit_conflict_blocks():
    """A conflicting-lesson finding BLOCKs the same way a near-duplicate does."""
    run_audit = lambda: {"n_findings": 1, "findings": [
        {"action": "merge", "slugs": ["always-X", "never-X"],
         "reason": "these two lessons directly contradict each other"}]}
    res = mg.audit_gate(run_audit=run_audit)
    assert res["block"] is True
    assert "contradict" in res["message"]


def test_audit_count_only_still_blocks():
    """Even when the runner gives only a count (no findings list), a positive
    count BLOCKs — the default --audit --dry-run runner returns n_findings."""
    res = mg.audit_gate(run_audit=lambda: {"n_findings": 2})
    assert res["block"] is True
    assert "2 finding" in res["message"]


def test_audit_default_runner_is_overridable():
    """audit_gate() with no runner falls back to the real one; we don't call it
    here (it would spend), just assert the default is wired and injection works."""
    assert callable(mg._default_audit_runner)
    # injected runner must take precedence over the default
    res = mg.audit_gate(run_audit=lambda: {"n_findings": 0})
    assert res["block"] is False


# ─── meta-change detection (FFP rules / skills ONLY) ─────────────────────────

@pytest.mark.parametrize("path", [
    "firefly-platform/.claude/rules/ai.md",
    ".claude/rules/ffp-lessons.md",
    "firefly-platform/.claude/skills/squirrel-code-review/SKILL.md",
    ".claude/skills/foo/SKILL.md",
])
def test_is_meta_path_positive(path):
    assert mg.is_meta_path(path) is True


@pytest.mark.parametrize("path", [
    # architect-ffp's OWN rules/skills/prelude — out of scope (future /archself)
    "skills/archffp/SKILL.md",
    "src/ffp-context.md",
    "prompts/observer-batch-prompt.md",
    # ordinary production / test / tooling paths
    "src/applications/squirrel/timeline.tsx",
    "src/scripts/bootstrap.py",
    "tests/test_meta_gate.py",
    "CHANGELOG.md",
])
def test_is_meta_path_negative(path):
    assert mg.is_meta_path(path) is False


def test_meta_paths_filters_to_ffp_rules_and_skills():
    paths = [
        "src/applications/squirrel/x.tsx",
        "firefly-platform/.claude/rules/ffp-lessons.md",
        "skills/archffp/SKILL.md",                 # architect-ffp skill — excluded
        ".claude/skills/squirrel-code-review/SKILL.md",
    ]
    assert mg.meta_paths(paths) == [
        "firefly-platform/.claude/rules/ffp-lessons.md",
        ".claude/skills/squirrel-code-review/SKILL.md",
    ]


def test_is_meta_change():
    assert mg.is_meta_change(["src/x.tsx", ".claude/rules/ffp-lessons.md"]) is True
    assert mg.is_meta_change(["src/x.tsx", "skills/archffp/SKILL.md"]) is False
    assert mg.is_meta_change([]) is False


# ─── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_detect_meta(capsys):
    rc = mg.main(["detect", "--paths", ".claude/rules/ffp-lessons.md", "src/x.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FFP rule/skill change detected" in out
    assert ".claude/rules/ffp-lessons.md" in out


def test_cli_detect_no_meta(capsys):
    rc = mg.main(["detect", "--paths", "src/x.py", "skills/archffp/SKILL.md"])
    out = capsys.readouterr().out
    assert rc == 10
    assert "SKIP" in out


def test_cli_check_skip_when_no_meta(capsys):
    """check with no meta path exits 0 ($0) and does not run the audit."""
    rc = mg.main(["check", "--paths", "src/x.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIP" in out
