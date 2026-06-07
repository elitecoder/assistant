"""Tests for bin/meta_gate.py — the archffp meta-gate.

The gate runs three checks over a rules/skills/prompts diff: regression (Observer
evals hold their verdicts), coverage (every new rule has a fixture), and audit (no
near-duplicate lessons). Gate 1 and Gate 3 do real Bedrock/LLM work in production,
so they take INJECTED runners here — every test below is hermetic: no `claude`
call, no network, a tmp fixtures dir. Same pattern test_lesson_extractor.py uses to
test extract() with an injected llm.
"""
from __future__ import annotations

import importlib.util
import json
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


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_fixture(fixtures_dir: Path, name: str, *, title: str = "",
                  notes: str = "", transcript: str = "", verdict: str = "active") -> Path:
    d = fixtures_dir / name
    d.mkdir(parents=True)
    (d / "ctx.json").write_text(json.dumps({"ws_ref": "workspace:1", "title": title}))
    (d / "expected.json").write_text(json.dumps({"verdict": verdict}))
    (d / "transcript.jsonl").write_text(transcript)
    if notes:
        (d / "notes.md").write_text(notes)
    return d


def _rule_file(tmp_path: Path, trigger: str, body: str = "do the thing") -> Path:
    p = tmp_path / "ffp-lessons.md"
    p.write_text(f"# Lessons\n\n**{trigger}**\n\n{body}\n")
    return p


# ─── Gate 1: regression ──────────────────────────────────────────────────────

def test_gate1_no_regression():
    """All fixtures hold their expected verdict → Gate 1 passes."""
    run_eval = lambda: {"failures": [], "total": 13}
    res = mg.gate1_regression(run_eval=run_eval)
    assert res["ok"] is True
    assert res["block"] is False
    assert "13" in res["message"]


def test_gate1_verdict_flip():
    """A fixture changes verdict → Gate 1 BLOCKs and names the flip."""
    run_eval = lambda: {"total": 13, "failures": [
        {"name": "06-question-to-user", "expected": "needs_user", "got": "active"}]}
    res = mg.gate1_regression(run_eval=run_eval)
    assert res["block"] is True
    assert res["ok"] is False
    assert "06-question-to-user" in res["message"]
    assert "needs_user" in res["message"] and "active" in res["message"]


def test_gate1_parses_real_run_py_output():
    """The default runner's parser handles run.py's actual FAIL line format,
    including the DANGEROUS/FORBIDDEN shape."""
    stdout = (
        "[observer-eval] running 06-question-to-user ...\n"
        "[observer-eval]   FAIL: ...\n"
        "[observer-eval]\n=== 11/13 passed ===\n"
        "  FAIL 06-question-to-user: verdict='active' expected 'needs_user'  (full: {...})\n"
        "  FAIL 01-ws97-trap: DANGEROUS: verdict='ready_for_merge' is FORBIDDEN for this fixture\n"
    )
    flips = mg._parse_run_py_failures(stdout)
    names = {f["name"] for f in flips}
    assert names == {"06-question-to-user", "01-ws97-trap"}
    q = next(f for f in flips if f["name"] == "06-question-to-user")
    assert q["expected"] == "needs_user" and q["got"] == "active"
    danger = next(f for f in flips if f["name"] == "01-ws97-trap")
    assert danger["got"] == "ready_for_merge"


# ─── Gate 2: coverage ────────────────────────────────────────────────────────

def test_gate2_has_coverage(tmp_path):
    """A new rule whose trigger shares a distinctive token with an existing
    fixture is considered covered → Gate 2 passes, no skeleton."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _make_fixture(fixtures, "01-teardown-case",
                  title="archffp cleanup teardown port reconciliation",
                  notes="Covers the teardown STUDIO_PORT mismatch path.")
    rule = _rule_file(tmp_path,
                      "About to run cleanup teardown while two worktrees are alive")
    res = mg.gate2_coverage([rule], fixtures_dir=fixtures)
    assert res["ok"] is True
    assert res["block"] is False
    assert res["skeletons"] == []
    # the distinctive token "teardown" (or "cleanup") should be the match
    shared = {t for c in res["covered"] for m in c["matches"] for t in m["shared"]}
    assert "teardown" in shared or "cleanup" in shared


def test_gate2_no_coverage(tmp_path):
    """A new rule with no related fixture → Gate 2 BLOCKs and scaffolds a
    skeleton fixture with verdict FILL_IN."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _make_fixture(fixtures, "01-unrelated",
                  title="some completely different workspace about merging branches",
                  notes="nothing to do with the new rule")
    rule = _rule_file(tmp_path,
                      "Never invoke quantum flux capacitor recalibration heuristics")
    res = mg.gate2_coverage([rule], fixtures_dir=fixtures)
    assert res["block"] is True
    assert res["ok"] is False
    assert len(res["skeletons"]) == 1
    skel = Path(res["skeletons"][0])
    assert skel.is_dir()
    assert skel.name.endswith("-skeleton")
    expected = json.loads((skel / "expected.json").read_text())
    assert expected["verdict"] == "FILL_IN"
    assert (skel / "ctx.json").exists()
    assert (skel / "transcript.jsonl").read_text() == ""
    assert (skel / "README.md").exists()
    assert "no eval coverage" in res["message"]


def test_gate2_no_skeletons_flag(tmp_path):
    """create_skeletons=False reports the gap without writing files."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _make_fixture(fixtures, "01-unrelated", title="merging branches")
    rule = _rule_file(tmp_path, "Never invoke quantum flux capacitor recalibration")
    res = mg.gate2_coverage([rule], fixtures_dir=fixtures, create_skeletons=False)
    assert res["block"] is True
    assert res["skeletons"] == []
    assert not list(fixtures.glob("*-skeleton"))


# ─── Gate 3: lesson audit ────────────────────────────────────────────────────

def test_gate3_clean_audit():
    """Audit finds nothing → Gate 3 passes."""
    run_audit = lambda: {"n_findings": 0, "findings": []}
    res = mg.gate3_audit(run_audit=run_audit)
    assert res["ok"] is True
    assert res["block"] is False


def test_gate3_duplicate_found():
    """Audit reports a near-duplicate → Gate 3 BLOCKs with the finding text."""
    run_audit = lambda: {"n_findings": 1, "findings": [
        {"action": "merge", "slugs": ["commit-work", "always-commit"],
         "reason": "both say commit when a unit of work finishes"}]}
    res = mg.gate3_audit(run_audit=run_audit)
    assert res["block"] is True
    assert res["ok"] is False
    assert "Lesson audit found issues" in res["message"]
    assert "commit-work" in res["message"]


def test_gate3_count_only_still_blocks():
    """Even when the runner gives only a count (no findings list), a positive
    count BLOCKs — the default --audit --dry-run runner returns n_findings."""
    res = mg.gate3_audit(run_audit=lambda: {"n_findings": 2})
    assert res["block"] is True
    assert "2 finding" in res["message"]


# ─── meta-change detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "firefly-platform/.claude/rules/squirrel/move.md",
    ".claude/rules/archffp-lessons.md",
    "skills/archffp/SKILL.md",
    "src/ffp-context.md",
    "prompts/observer-batch-prompt.md",
    "some/nested/prompts/thing.md",
])
def test_is_meta_path_positive(path):
    assert mg.is_meta_path(path) is True


@pytest.mark.parametrize("path", [
    "src/scripts/bootstrap.py",
    "tests/test_meta_gate.py",
    "evals/run.py",
    "CHANGELOG.md",
    "src/e2e_manager/manager.py",
])
def test_is_meta_path_negative(path):
    assert mg.is_meta_path(path) is False


def test_is_meta_change_mixed():
    paths = ["src/scripts/bootstrap.py", "tests/x.py", ".claude/rules/ffp-lessons.md"]
    assert mg.is_meta_change(paths) is True
    assert mg.meta_paths(paths) == [".claude/rules/ffp-lessons.md"]
    assert mg.is_meta_change(["src/scripts/bootstrap.py", "tests/x.py"]) is False


# ─── trigger extraction ──────────────────────────────────────────────────────

def test_extract_rule_triggers_bold():
    text = (
        "# architect-ffp Lessons\n\n## Lessons\n\n"
        "**About to run cleanup teardown while two worktrees are alive**\n\n"
        "Some rule body here.\n\n"
        "**Never self-modify the permission allowlist**\n\n"
        "**Why:** because it is dangerous\n"
        "**How to apply:** surface it back\n"
    )
    trigs = mg.extract_rule_triggers(text)
    assert "About to run cleanup teardown while two worktrees are alive" in trigs
    assert "Never self-modify the permission allowlist" in trigs
    # Why / How to apply are body markers, not triggers
    assert not any(t.lower().startswith("why") for t in trigs)
    assert not any(t.lower().startswith("how to apply") for t in trigs)


def test_extract_rule_triggers_header_fallback():
    text = "## Some heading rule\n\nbody\n\n### Another one\n\nbody\n"
    trigs = mg.extract_rule_triggers(text)
    assert "Some heading rule" in trigs
    assert "Another one" in trigs


def test_slugify():
    assert mg.slugify("About to run cleanup teardown!") == "about-to-run-cleanup-teardown"
    assert len(mg.slugify("x" * 100)) <= 40


# ─── orchestration: sequence + short-circuit ─────────────────────────────────

def test_run_all_gates_stops_at_first_block(tmp_path):
    """Gate 1 BLOCK short-circuits — Gate 2/3 never run."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    rule = _rule_file(tmp_path, "Some trigger")
    audit_called = {"n": 0}

    def _audit():
        audit_called["n"] += 1
        return {"n_findings": 0}

    res = mg.run_all_gates(
        [rule], fixtures_dir=fixtures,
        run_eval=lambda: {"failures": [{"name": "f", "expected": "active", "got": "stranded"}]},
        run_audit=_audit,
    )
    assert res["block"] is True
    assert res["stopped_at"] == 1
    assert audit_called["n"] == 0  # never reached Gate 3


def test_run_all_gates_all_pass(tmp_path):
    """All three green → no block, canonical success message for the SKILL.md log."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _make_fixture(fixtures, "01-cov", title="teardown cleanup port", notes="teardown")
    rule = _rule_file(tmp_path, "About to run cleanup teardown reconciliation")
    res = mg.run_all_gates(
        [rule], fixtures_dir=fixtures,
        run_eval=lambda: {"failures": [], "total": 13},
        run_audit=lambda: {"n_findings": 0},
    )
    assert res["block"] is False
    assert res["message"] == "meta-gate: all three checks pass"
    assert [r["gate"] for r in res["results"]] == [1, 2, 3]


def test_run_all_gates_skip_regression(tmp_path):
    """--skip-regression omits Gate 1 entirely (the Bedrock-spending one)."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _make_fixture(fixtures, "01-cov", title="teardown cleanup", notes="teardown")
    rule = _rule_file(tmp_path, "About to run cleanup teardown")
    res = mg.run_all_gates(
        [rule], fixtures_dir=fixtures, skip_regression=True,
        run_audit=lambda: {"n_findings": 0},
    )
    assert res["block"] is False
    assert [r["gate"] for r in res["results"]] == [2, 3]
