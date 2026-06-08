"""Tests for bin/meta_gate.py — the archffp --meta gate (4-gate TDD discipline).

This module owns the pure, testable pieces of the meta-gate: detection, Gate 1
(dedup/conflict, injected LLM), and Gate 4's coverage search. Gate 2 (do the
work) and Gate 3 (run the live archffp evals) are orchestrator-driven and not
unit-tested here. Every test is hermetic — the Gate-1 LLM is injected, so no
`claude`/Bedrock call ever runs (same pattern test_lesson_extractor.py uses).
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

CORPUS = {
    "lessons": [
        {"store": "ffp", "slug": "commit-work", "trigger": "Always commit when a unit of work is finished"},
        {"store": "claude", "slug": "no-force-push", "trigger": "Never force-push without permission"},
    ],
    "skills": ["squirrel-code-review", "e2e-test"],
}


# ─── Gate 1: dedup / conflict ────────────────────────────────────────────────

def test_gate1_no_duplicate_passes():
    llm = lambda _p: json.dumps({"duplicate": False, "conflict": False, "matches": []})
    res = mg.dedup_gate("New rule: prefer Scout over grep for code search", CORPUS, run_llm=llm)
    assert res["ok"] is True
    assert res["block"] is False


def test_gate1_duplicate_blocks():
    llm = lambda _p: json.dumps({"duplicate": True, "conflict": False, "matches": [
        {"kind": "duplicate", "ref": "ffp/commit-work", "why": "same 'commit finished work' intent"}]})
    res = mg.dedup_gate("Always commit your work when done", CORPUS, run_llm=llm)
    assert res["block"] is True
    assert res["ok"] is False
    assert "ffp/commit-work" in res["message"]
    assert "duplicate" in res["message"].lower()


def test_gate1_conflict_blocks():
    llm = lambda _p: json.dumps({"duplicate": False, "conflict": True, "matches": [
        {"kind": "conflict", "ref": "claude/no-force-push", "why": "proposed rule allows force-push"}]})
    res = mg.dedup_gate("Force-push freely to feature branches", CORPUS, run_llm=llm)
    assert res["block"] is True
    assert "no-force-push" in res["message"]


def test_gate1_matches_without_flags_still_blocks():
    """If the LLM returns matches but forgets the boolean flags, still BLOCK."""
    llm = lambda _p: json.dumps({"matches": [
        {"kind": "duplicate", "ref": "ffp/commit-work", "why": "same intent"}]})
    res = mg.dedup_gate("commit finished work", CORPUS, run_llm=llm)
    assert res["block"] is True


def test_gate1_prompt_includes_corpus_and_proposed():
    """The dedup prompt must actually carry the proposed change + the existing
    lessons/skills, or the LLM is judging blind."""
    captured = {}
    def llm(p):
        captured["prompt"] = p
        return json.dumps({"duplicate": False, "conflict": False, "matches": []})
    mg.dedup_gate("PROPOSED-RULE-XYZ", CORPUS, run_llm=llm)
    p = captured["prompt"]
    assert "PROPOSED-RULE-XYZ" in p
    assert "commit-work" in p              # existing lesson surfaced
    assert "squirrel-code-review" in p     # existing skill surfaced


def test_gate1_unparseable_llm_does_not_falsely_block():
    """A tooling failure (bad JSON) is surfaced, not turned into a phantom block."""
    res = mg.dedup_gate("anything", CORPUS, run_llm=lambda _p: "not json at all")
    assert res["block"] is False
    assert "error" in res


# ─── Gate 4: coverage search ─────────────────────────────────────────────────

def _fixture(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_gate4_covered(tmp_path):
    """A fixture whose input mentions the change → covered."""
    fx = tmp_path / "fixtures"
    _fixture(fx, "snapping-rule/code-reviewer/input/diff.md",
             "rule about timeline snapping threshold during gapless reorder")
    res = mg.coverage_search(["timeline snapping threshold"], fixtures_dir=fx)
    assert res["covered"] is True
    assert res["matches"]
    assert "snapping" in {t for m in res["matches"] for t in m["shared"]}


def test_gate4_not_covered_signals_write_new_fixture(tmp_path):
    """No fixture mentions the change → NOT covered (TDD: write one)."""
    fx = tmp_path / "fixtures"
    _fixture(fx, "unrelated/classifier/input/x.md", "completely different drag clone behavior")
    res = mg.coverage_search(["quantum flux capacitor recalibration"], fixtures_dir=fx)
    assert res["covered"] is False
    assert res["matches"] == []
    assert "write one" in res["message"]


def test_gate4_empty_fixtures_dir(tmp_path):
    res = mg.coverage_search(["anything"], fixtures_dir=tmp_path / "nope")
    assert res["covered"] is False


def test_gate4_accepts_freetext_description(tmp_path):
    fx = tmp_path / "fixtures"
    _fixture(fx, "f/code-reviewer/input/d.md", "magic number literal banned in production code")
    res = mg.coverage_search("ban magic number literals", fixtures_dir=fx)
    assert res["covered"] is True


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
    "skills/archffp/SKILL.md",       # architect-ffp's OWN skill — future /archself
    "src/ffp-context.md",
    "src/applications/squirrel/timeline.tsx",
    "tests/test_meta_gate.py",
    "CHANGELOG.md",
])
def test_is_meta_path_negative(path):
    assert mg.is_meta_path(path) is False


def test_meta_paths_and_is_meta_change():
    paths = ["src/x.tsx", ".claude/rules/ffp-lessons.md", "skills/archffp/SKILL.md"]
    assert mg.meta_paths(paths) == [".claude/rules/ffp-lessons.md"]
    assert mg.is_meta_change(paths) is True
    assert mg.is_meta_change(["src/x.tsx", "skills/archffp/SKILL.md"]) is False


# ─── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_detect_meta(capsys):
    rc = mg.main(["detect", "--paths", ".claude/rules/ffp-lessons.md", "src/x.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FFP rule/skill change detected" in out


def test_cli_detect_not_meta(capsys):
    rc = mg.main(["detect", "--paths", "src/x.py", "skills/archffp/SKILL.md"])
    assert rc == 10
    assert "not a meta-change" in capsys.readouterr().out


def test_cli_coverage_miss(tmp_path, capsys):
    rc = mg.main(["coverage", "--keywords", "nonexistent-topic-zzz",
                  "--fixtures-dir", str(tmp_path)])
    assert rc == 10  # not covered → nonzero so the orchestrator knows to write a fixture


def test_cli_coverage_hit_json(tmp_path, capsys):
    """--json flag emits JSON rather than human text."""
    fx = tmp_path / "fixtures"
    fx.mkdir()
    (fx / "f.md").write_text("snapping threshold coverage text")
    rc = mg.main(["coverage", "--keywords", "snapping", "--fixtures-dir", str(fx), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["covered"] is True


def test_cli_coverage_hit_text(tmp_path, capsys):
    """Without --json flag, text output includes fixture paths."""
    fx = tmp_path / "fixtures"
    fx.mkdir()
    (fx / "x.md").write_text("timeline ruler coverage text")
    rc = mg.main(["coverage", "--keywords", "timeline", "--fixtures-dir", str(fx)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "x.md" in out


def test_cli_detect_with_repo(tmp_path, capsys, monkeypatch):
    """--repo flag calls _git_changed_paths and appends to the explicit path list."""
    monkeypatch.setattr(mg, "_git_changed_paths",
                        lambda repo, base: [".claude/rules/ffp-lessons.md"])
    rc = mg.main(["detect", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FFP rule/skill change" in out


def test_git_changed_paths_returns_sorted(tmp_path, monkeypatch):
    """_git_changed_paths unions diff, unstaged, and staged output."""
    import subprocess as sp

    class _CP:
        returncode = 0

    def fake_run(cmd, *a, **kw):
        r = _CP()
        if "--staged" in cmd:
            r.stdout = ".claude/rules/ffp-lessons.md\n"
        elif cmd[-1] == "HEAD..HEAD":  # never matches, but exercises the loop
            r.stdout = ""
        else:
            r.stdout = ".claude/rules/ffp-lessons.md\nsrc/x.tsx\n"
        return r

    monkeypatch.setattr(mg.subprocess, "run", fake_run)
    paths = mg._git_changed_paths(tmp_path, "origin/main")
    assert ".claude/rules/ffp-lessons.md" in paths
    assert paths == sorted(paths)


def test_gather_existing_corpus_subprocess_ok(monkeypatch):
    """gather_existing_corpus parses curator list output."""
    import subprocess as sp

    class _CP:
        returncode = 0
        stdout = "  [global         ] commit-work                                          2026-01-01\n"

    monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _CP())
    corpus = mg.gather_existing_corpus()
    assert "lessons" in corpus
    assert "skills" in corpus


def test_gather_existing_corpus_subprocess_error(monkeypatch):
    """gather_existing_corpus handles subprocess error gracefully."""
    import subprocess as sp

    class _CP:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _CP())
    corpus = mg.gather_existing_corpus()
    assert corpus["lessons"] == []
