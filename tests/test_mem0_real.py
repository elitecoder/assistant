"""Integration tests for the REAL mem0ai-backed memory path.

Companion to test_mem0_tools.py: that file pins the dependency-free local JSONL
fallback (MEM0_FORCE_LOCAL=1) and runs everywhere; this file exercises the
actual mem0ai backend — chroma vector store + embeddings — and `skip`s when
mem0ai isn't importable in the current interpreter (per the spec's
`pytest.importorskip("mem0")`).

mem0ai lives in the dedicated .venv-mem0 (Python 3.12), so these run for real
only under that interpreter:

    .venv-mem0/bin/python -m pytest tests/test_mem0_real.py -v

Under the system python3 (no mem0) every test here skips cleanly. The store is
isolated to tmp_path (monkeypatching mem0_backend.MEM0_DATA_DIR) so a test run
never touches Mukul's production memories at ~/.assistant/mem0/. Assertions are
on behavior — add returns an id, search finds the relevant memory, a re-add is a
no-op — so they hold whether the live embedder is Bedrock or the fastembed
fallback.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("mem0")  # skip the whole module unless mem0ai is installed

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "bin/tools/mem0_backend.py"
SEEDS = REPO / "bin/tools/memory_seeds.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """A MemoryBackend whose vector store is isolated under tmp_path. Skips if
    the real mem0 store can't be built here (no embedder reachable at all)."""
    mb = _load("mem0_backend_real", BACKEND)
    monkeypatch.delenv("MEM0_FORCE_LOCAL", raising=False)
    monkeypatch.setattr(mb, "MEM0_DATA_DIR", tmp_path / "mem0")
    monkeypatch.setattr(mb, "LOCAL_STORE", tmp_path / "mem0" / "memories.jsonl")
    b = mb.MemoryBackend()
    if b._real is None:
        pytest.skip("real mem0 backend unavailable (no reachable embedder)")
    return b


def test_mem0_add_and_search(backend):
    res = backend.add("Mukul approved merging P8 before P6.2 when CI is all-green",
                      {"category": "decision", "project": "connections"})
    assert res["status"] == "added"
    assert res["memory_id"]
    assert res["provider"].startswith("mem0ai")

    hits = backend.search("What did Mukul decide about merging P8?", limit=5)
    assert hits, "expected at least one hit for the seeded decision"
    assert any("P8" in h["memory"] for h in hits)


def test_mem0_add_is_idempotent(backend):
    a = backend.add("verbatim idempotency probe", {"category": "decision"})
    b = backend.add("verbatim idempotency probe", {"category": "decision"})
    assert a["status"] == "added"
    assert b["status"] == "exists"
    assert a["memory_id"] == b["memory_id"]


def test_mem0_search_returns_relevant(backend):
    backend.add("Squirrel timeline uses Lit web components with MobX state",
                {"category": "project"})
    backend.add("Mukul prefers terse replies, not bullet walls",
                {"category": "working_style"})
    hits = backend.search("which UI framework does the timeline editor use", limit=1)
    assert len(hits) == 1
    assert "Lit web components" in hits[0]["memory"]


def test_mem0_search_category_filter(backend):
    backend.add("brevity note filed under working style", {"category": "working_style"})
    backend.add("brevity note filed under project", {"category": "project"})
    hits = backend.search("brevity", limit=5, category="project")
    assert hits
    assert all(h["metadata"].get("category") == "project" for h in hits)


def test_mem0_seed_lessons_matches_confirmed(backend, tmp_path, monkeypatch):
    """Seeding confirmed lessons into the real backend writes exactly one memory
    per confirmed proposal, and a re-seed is a no-op."""
    seeds = _load("memory_seeds_real", SEEDS)
    proposals = tmp_path / "proposals.jsonl"
    rows = [
        {"type": "lesson", "status": "confirmed", "trigger": "T1", "rule": "R1",
         "scope": "general", "target": "claude"},
        {"type": "lesson", "status": "confirmed", "trigger": "T2", "rule": "R2",
         "scope": "ffp", "target": "assistant"},
        {"type": "lesson", "status": "pending", "trigger": "T3", "rule": "R3"},
    ]
    proposals.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(seeds, "PROPOSALS", proposals)

    confirmed = seeds.confirmed_lessons()
    assert len(confirmed) == 2  # the pending one is excluded

    for c in confirmed:
        r = backend.add(c["content"], {"category": "lesson", **c["frontmatter"]})
        assert r["status"] == "added"
    # Re-seed: every one is now a duplicate.
    for c in confirmed:
        r = backend.add(c["content"], {"category": "lesson", **c["frontmatter"]})
        assert r["status"] == "exists"

    hits = backend.search("R1", category="lesson", limit=5)
    assert any("R1" in h["memory"] for h in hits)
