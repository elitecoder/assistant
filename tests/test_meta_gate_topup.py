"""Coverage top-up for bin/meta_gate.py — fills the branches the existing
tests/test_meta_gate.py leaves open:

  - gather_existing_corpus: curator output with '|', '#' comments, blank lines
    parsed; a target whose curator call returns rc!=0 skipped; a skills dir with
    subdirs listed; a missing skills dir → [].
  - dedup_gate defaults: corpus=None → gather_existing_corpus invoked;
    run_llm=None → defaults to _claude_oneshot (stubbed via module patch).
  - _claude_oneshot: points LESSON_EXTRACTOR at a tiny tmp .py that defines
    _claude_oneshot → no real LLM.
  - coverage_search: free-text vs list keywords, non-md/json/txt file ignored,
    an unreadable file (OSError) skipped, empty fixtures_dir → not covered.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, fname: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(REPO / "bin" / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mg = _load("meta_gate_topup_mod", "meta_gate.py")


# ─── gather_existing_corpus parsing branches ─────────────────────────────────

def test_gather_corpus_parses_pipe_comment_and_blank(monkeypatch, tmp_path):
    """A curator listing with a piped slug|trigger line, a '#' comment, and a
    blank line: the pipe line is parsed (slug + trigger split), comment/blank
    are skipped. rc!=0 targets are skipped entirely."""
    import subprocess as sp

    class _CP:
        def __init__(self, rc, stdout):
            self.returncode = rc
            self.stdout = stdout

    calls = {"n": 0}

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        target = cmd[cmd.index("--target") + 1]
        if target == "claude":
            return _CP(0,
                       "commit-work | Always commit finished work\n"
                       "# this is a comment\n"
                       "\n"
                       "plain-line-no-pipe\n")
        if target == "ffp":
            return _CP(1, "boom")  # rc!=0 → skipped
        return _CP(0, "")  # other targets: empty

    monkeypatch.setattr(mg.subprocess, "run", fake_run)
    skills_dir = tmp_path / "skills"
    (skills_dir / "squirrel-review").mkdir(parents=True)
    (skills_dir / "e2e-test").mkdir(parents=True)
    (skills_dir / "not-a-dir.txt").write_text("ignore me")

    corpus = mg.gather_existing_corpus(ffp_skills_dir=skills_dir)

    slugs = {l["slug"] for l in corpus["lessons"]}
    assert "commit-work" in slugs
    # The piped line split slug from trigger.
    commit = next(l for l in corpus["lessons"] if l["slug"] == "commit-work")
    assert commit["trigger"] == "Always commit finished work"
    assert commit["store"] == "claude"
    # Plain (no-pipe) line: whole line becomes the trigger, slug is the line.
    plain = next(l for l in corpus["lessons"] if l["slug"] == "plain-line-no-pipe")
    assert plain["trigger"] == "plain-line-no-pipe"
    # Skills dir: only directories, sorted.
    assert corpus["skills"] == ["e2e-test", "squirrel-review"]


def test_gather_corpus_missing_skills_dir(monkeypatch, tmp_path):
    import subprocess as sp

    class _CP:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(mg.subprocess, "run", lambda *a, **k: _CP())
    corpus = mg.gather_existing_corpus(ffp_skills_dir=tmp_path / "nope")
    assert corpus["skills"] == []


def test_gather_corpus_subprocess_oserror_skipped(monkeypatch, tmp_path):
    """If the curator subprocess itself raises OSError, that target is skipped
    (the except clause), not crashed."""
    def boom(*a, **k):
        raise OSError("no curator binary")

    monkeypatch.setattr(mg.subprocess, "run", boom)
    corpus = mg.gather_existing_corpus(ffp_skills_dir=tmp_path / "nope")
    assert corpus["lessons"] == []
    assert corpus["skills"] == []


# ─── dedup_gate defaults: corpus=None, run_llm=None ──────────────────────────

def test_dedup_gate_default_corpus(monkeypatch):
    """corpus=None → dedup_gate calls gather_existing_corpus."""
    monkeypatch.setattr(mg, "gather_existing_corpus",
                        lambda *a, **k: {"lessons": [], "skills": ["seeded-skill"]})
    captured = {}

    def llm(p):
        captured["prompt"] = p
        return json.dumps({"duplicate": False, "conflict": False, "matches": []})

    res = mg.dedup_gate("a brand new rule", corpus=None, run_llm=llm)
    assert res["ok"] is True
    assert res["block"] is False
    # The seeded corpus must have reached the prompt.
    assert "seeded-skill" in captured["prompt"]


def test_dedup_gate_default_run_llm(monkeypatch):
    """run_llm=None → dedup_gate falls back to mg._claude_oneshot. We stub that
    module-level function so no real LLM runs."""
    monkeypatch.setattr(mg, "_claude_oneshot",
                        lambda p: json.dumps({"duplicate": False, "conflict": False, "matches": []}))
    res = mg.dedup_gate("rule text", corpus={"lessons": [], "skills": []}, run_llm=None)
    assert res["ok"] is True
    assert res["block"] is False


def test_dedup_gate_default_run_llm_duplicate_blocks(monkeypatch):
    monkeypatch.setattr(mg, "_claude_oneshot",
                        lambda p: json.dumps({"duplicate": True, "conflict": False,
                                              "matches": [{"kind": "duplicate",
                                                           "ref": "ffp/x", "why": "same"}]}))
    res = mg.dedup_gate("dup rule", corpus={"lessons": [], "skills": []}, run_llm=None)
    assert res["block"] is True
    assert "ffp/x" in res["message"]


# ─── _claude_oneshot via a tiny stub lesson-extractor ────────────────────────

def test_claude_oneshot_delegates_to_lesson_extractor(monkeypatch, tmp_path):
    """_claude_oneshot importlib-loads LESSON_EXTRACTOR and calls its
    _claude_oneshot. Point it at a tiny module that returns a fixed string."""
    stub = tmp_path / "stub_extractor.py"
    stub.write_text(
        "def _claude_oneshot(prompt):\n"
        "    return '{\"ok\": true}'\n"
    )
    monkeypatch.setattr(mg, "LESSON_EXTRACTOR", stub)
    out = mg._claude_oneshot("any prompt")
    assert out == '{"ok": true}'


# ─── coverage_search branches ────────────────────────────────────────────────

def _fixture(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_coverage_search_freetext_string(tmp_path):
    """keywords passed as a free-text string (not a list) still tokenizes and
    matches."""
    fx = tmp_path / "fixtures"
    _fixture(fx, "cell/input/d.md", "magic number literal banned in production code")
    res = mg.coverage_search("ban magic number literals", fixtures_dir=fx)
    assert res["covered"] is True
    assert res["matches"]


def test_coverage_search_list_keywords(tmp_path):
    fx = tmp_path / "fixtures"
    _fixture(fx, "cell/input/d.md", "timeline snapping threshold during gapless reorder")
    res = mg.coverage_search(["timeline snapping threshold"], fixtures_dir=fx)
    assert res["covered"] is True
    assert "snapping" in {t for m in res["matches"] for t in m["shared"]}


def test_coverage_search_ignores_non_text_extensions(tmp_path):
    """A .png (or other non md/json/txt) file is never scanned even if its name
    overlaps the keywords — only .md/.json/.txt are searched."""
    fx = tmp_path / "fixtures"
    _fixture(fx, "cell/snapping.png", "snapping snapping snapping")
    res = mg.coverage_search(["snapping"], fixtures_dir=fx)
    assert res["covered"] is False
    assert res["matches"] == []
    assert "write one" in res["message"]


def test_coverage_search_skips_unreadable_file(tmp_path):
    """A file whose read_text raises OSError is skipped, not crashed; the
    readable file still matches."""
    fx = tmp_path / "fixtures"
    _fixture(fx, "a/good.md", "snapping threshold coverage")
    _fixture(fx, "b/bad.md", "snapping threshold also here")

    real_read = Path.read_text

    def flaky_read(self, *a, **k):
        if self.name == "bad.md":
            raise OSError("disk error")
        return real_read(self, *a, **k)

    with mock.patch.object(Path, "read_text", flaky_read):
        res = mg.coverage_search(["snapping threshold"], fixtures_dir=fx)
    assert res["covered"] is True
    fixtures = {m["fixture"] for m in res["matches"]}
    assert any("good.md" in f for f in fixtures)
    assert not any("bad.md" in f for f in fixtures)


def test_coverage_search_empty_dir_not_covered(tmp_path):
    res = mg.coverage_search(["anything"], fixtures_dir=tmp_path / "nope")
    assert res["covered"] is False
    assert res["matches"] == []


def test_coverage_search_no_overlap_not_covered(tmp_path):
    fx = tmp_path / "fixtures"
    _fixture(fx, "x/y.md", "completely different drag clone behavior")
    res = mg.coverage_search(["quantum flux capacitor recalibration"], fixtures_dir=fx)
    assert res["covered"] is False
