"""Integration tests for the mem0 memory tools.

These exercise the REAL bin/tools/mem0_backend.py via the local JSONL backend
(MEM0_FORCE_LOCAL=1), so they run deterministically without mem0ai installed —
the local store is the spec's sanctioned fallback. add/search/idempotency/
category-filter and seed-from-confirmed-lessons are covered against an isolated
store in tmp_path. The day mem0ai + an embedder land, the same CLI keeps these
contracts; only the ranking improves.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "bin/tools/mem0_backend.py"
SEEDS = REPO / "bin/tools/memory_seeds.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def force_local(monkeypatch):
    monkeypatch.setenv("MEM0_FORCE_LOCAL", "1")


@pytest.fixture
def store(tmp_path):
    mb = _load("mem0_backend_mod", BACKEND)
    s = mb.LocalStore(tmp_path / "memories.jsonl")
    return mb, s


def test_mem0_add_and_search(store):
    mb, s = store
    res = s.add("Mukul prefers terse executive-summary replies",
                {"category": "working_style"})
    assert res["status"] == "added"
    assert res["memory_id"]
    hits = s.search("terse summary replies", limit=5)
    assert hits and hits[0]["memory"].startswith("Mukul prefers terse")
    assert hits[0]["score"] > 0


def test_mem0_add_is_idempotent(store):
    mb, s = store
    a = s.add("same content", {"category": "decision", "project": "x"})
    b = s.add("same content", {"category": "decision", "project": "x"})
    assert a["memory_id"] == b["memory_id"]
    assert a["status"] == "added"
    assert b["status"] == "exists"
    # Only one line on disk.
    lines = [ln for ln in s.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_mem0_search_returns_relevant(store):
    mb, s = store
    s.add("Squirrel timeline uses Lit web components with MobX",
          {"category": "project"})
    s.add("Mukul approved merging P8 before P6.2 when CI is all-green",
          {"category": "decision"})
    hits = s.search("which framework does the timeline use", limit=1)
    assert len(hits) == 1
    assert "Lit web components" in hits[0]["memory"]


def test_mem0_search_category_filter(store):
    mb, s = store
    s.add("a working style note about brevity", {"category": "working_style"})
    s.add("a project note about brevity", {"category": "project"})
    hits = s.search("brevity", limit=5, category="project")
    assert len(hits) == 1
    assert hits[0]["metadata"]["category"] == "project"


def test_mem0_search_miss_returns_empty(store):
    mb, s = store
    s.add("something entirely about apples", {"category": "decision"})
    assert s.search("zzznonexistent quantum chromodynamics", limit=5) == []


def test_mem0_seed_lessons_matches_confirmed(store, monkeypatch, tmp_path):
    """Seeding from confirmed lessons writes exactly one memory per confirmed
    proposal — drive memory_seeds.confirmed_lessons against a sandbox file."""
    mb, s = store
    seeds = _load("memory_seeds_mod", SEEDS)
    proposals = tmp_path / "proposals.jsonl"
    rows = [
        {"type": "lesson", "status": "confirmed", "trigger": "T1",
         "rule": "R1", "scope": "general", "target": "claude"},
        {"type": "lesson", "status": "confirmed", "trigger": "T2",
         "rule": "R2", "scope": "ffp", "target": "assistant"},
        {"type": "lesson", "status": "pending", "trigger": "T3", "rule": "R3"},
    ]
    proposals.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(seeds, "PROPOSALS", proposals)

    confirmed = seeds.confirmed_lessons()
    assert len(confirmed) == 2  # the pending one is excluded
    for c in confirmed:
        assert c["category"] == "lesson"
        s.add(c["content"], {"category": "lesson", **c["frontmatter"]})
    lines = [ln for ln in s.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
