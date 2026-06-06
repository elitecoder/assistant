"""Tests for bin/lesson-extractor.py.

Covers the pure pattern-detection + dedup logic, plus the full extract()
pipeline driven by an INJECTED llm (so no `claude -p` ever runs) writing to a
tmp proposals.jsonl. Idempotency — the headline contract — is asserted by
running extract() twice over the same ledger and confirming the second pass
writes nothing new.
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


le = _load("lesson_extractor_mod", "lesson-extractor.py")


# ─── helpers ─────────────────────────────────────────────────────────────────

def _entry(kind: str, evidence: str, *, epoch: int, ws: str = "workspace:1",
           outcome: str = "verified") -> dict:
    return {"ts": "2026-06-06T00:00:00Z", "epoch": epoch, "kind": kind,
            "ws_ref": ws, "outcome": outcome, "evidence": evidence}


def _write_ledger(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


# A stub LLM: always returns a usable draft. Records how many times it ran.
class StubLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps({
            "trigger": f"pattern #{self.calls}",
            "rule": "Do the right thing because the pattern says so.",
            "target": "assistant", "scope": "general",
        })


# ─── evidence_stem (pure) ────────────────────────────────────────────────────

def test_stem_strips_ws_refs_and_numbers():
    a = le.evidence_stem("sent /cleanup to workspace:4 delta=12")
    b = le.evidence_stem("sent /cleanup to workspace:90 delta=8")
    assert a == b
    assert "workspace" not in a and "4" not in a


def test_stem_strips_pr_and_td():
    s = le.evidence_stem("merged PR #11042 for td-084 on workspace:12")
    assert "#11042" not in s and "td-084" not in s and "workspace" not in s


# ─── find_patterns (pure) ────────────────────────────────────────────────────

def test_find_patterns_needs_three_occurrences():
    entries = [
        _entry("stranded", "sent nudge to revive idle agent now", epoch=1000),
        _entry("stranded", "sent nudge to revive idle agent now", epoch=1001),
    ]
    assert le.find_patterns(entries, now=2000) == []  # only 2 → no candidate

    entries.append(_entry("stranded", "sent nudge to revive idle agent now", epoch=1002))
    cands = le.find_patterns(entries, now=2000)
    assert len(cands) == 1 and cands[0]["count"] == 3
    assert cands[0]["kind"] == "stranded"


def test_find_patterns_excludes_outside_window():
    old = 1000
    entries = [_entry("emit-card", "pr open awaiting reviewer approval still", epoch=old)
               for _ in range(3)]
    # now is far beyond old + 72h → all excluded.
    assert le.find_patterns(entries, now=old + 72 * 3600 + 1) == []


def test_find_patterns_ignores_unverified():
    entries = [_entry("cleanup", "tore down stranded husk workspace cleanly",
                      epoch=1000, outcome="failed") for _ in range(5)]
    assert le.find_patterns(entries, now=2000) == []


def test_find_patterns_groups_distinct_kinds_separately():
    entries = []
    for i in range(3):
        entries.append(_entry("stranded", "nudged idle agent to resume work", epoch=1000 + i))
    for i in range(3):
        entries.append(_entry("emit-card", "surfaced pr awaiting reviewer for user", epoch=1000 + i))
    cands = le.find_patterns(entries, now=2000)
    assert {c["kind"] for c in cands} == {"stranded", "emit-card"}


# ─── dedup ───────────────────────────────────────────────────────────────────

def test_is_duplicate_matches_existing_trigger():
    cand = {"stem": "sent cleanup to workspace husk", "kind": "cleanup", "count": 3}
    triggers = ["When you sent cleanup to workspace husk, double-check first"]
    assert le.is_duplicate(cand, triggers, set()) is True


def test_is_duplicate_matches_pending_stem():
    cand = {"stem": "nudged idle agent to resume", "kind": "stranded", "count": 3}
    pending = {le._norm("nudged idle agent to resume")}
    assert le.is_duplicate(cand, [], pending) is True


def test_is_not_duplicate_when_novel():
    cand = {"stem": "brand new never seen pattern here", "kind": "x", "count": 3}
    assert le.is_duplicate(cand, ["unrelated rule about merging"], set()) is False


# ─── extract() pipeline (injected llm, tmp paths) ────────────────────────────

def _empty_curator_runner(cmd):
    """Stand in for the curator list CLI — no existing lessons."""
    return (0, "(no lessons)\n", "")


def test_extract_writes_proposal_and_is_idempotent(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    proposals = tmp_path / "proposals.jsonl"
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(le, "AUDIT_LOG", audit)
    # No existing lessons in either store.
    monkeypatch.setattr(le, "existing_triggers", lambda *a, **k: [])

    _write_ledger(ledger, [
        _entry("stranded", "nudged idle agent to resume its task", epoch=1000 + i, ws=f"workspace:{i}")
        for i in range(4)
    ])
    stub = StubLLM()

    # tg-send must never actually run — stub the ping runner.
    sent = []

    def fake_ping_runner(cmd):
        sent.append(cmd)
        return (0, "", "")

    r1 = le.extract(llm=stub, ledger_path=ledger, proposals_path=proposals,
                    tg_send=tmp_path / "tg-send.py", now=2000,
                    curator=tmp_path / "curator.py")
    # Patch the ping runner indirectly: ping_user used the default _run, which
    # would try to exec a nonexistent tg-send — that returns rc!=0 but never
    # raises, so the proposal still gets written. Assert on the write.
    assert r1["n_proposed"] == 1
    assert proposals.exists()
    lines = [json.loads(x) for x in proposals.read_text().splitlines() if x.strip()]
    assert len(lines) == 1
    assert lines[0]["status"] == "pending"
    assert lines[0]["source"] == "extractor"
    assert lines[0]["type"] == "lesson"
    assert "pattern_stem" in lines[0]
    assert stub.calls == 1

    # Second pass over the SAME ledger: the pending proposal's stem dedups it.
    r2 = le.extract(llm=stub, ledger_path=ledger, proposals_path=proposals,
                    tg_send=tmp_path / "tg-send.py", now=2000,
                    curator=tmp_path / "curator.py")
    assert r2["n_proposed"] == 0
    assert r2["n_skipped"] == 1
    lines2 = [x for x in proposals.read_text().splitlines() if x.strip()]
    assert len(lines2) == 1, "idempotent: no duplicate proposal on re-run"


def test_extract_dry_run_writes_nothing(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    proposals = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(le, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(le, "existing_triggers", lambda *a, **k: [])
    _write_ledger(ledger, [
        _entry("cleanup", "tore down stranded husk after merge landed", epoch=1000 + i)
        for i in range(3)
    ])
    stub = StubLLM()
    r = le.extract(dry_run=True, llm=stub, ledger_path=ledger,
                   proposals_path=proposals, now=2000,
                   curator=tmp_path / "curator.py")
    assert r["dry_run"] is True
    assert r["n_proposed"] == 1
    assert not proposals.exists(), "dry-run must not write proposals.jsonl"


def test_extract_skips_when_llm_returns_garbage(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    proposals = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(le, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(le, "existing_triggers", lambda *a, **k: [])
    _write_ledger(ledger, [
        _entry("stranded", "nudged idle agent to resume work now", epoch=1000 + i)
        for i in range(3)
    ])
    r = le.extract(llm=lambda prompt: "sorry I cannot do that",
                   ledger_path=ledger, proposals_path=proposals, now=2000,
                   curator=tmp_path / "curator.py")
    assert r["n_proposed"] == 0
    assert r["n_skipped"] == 1
    assert r["skipped"][0]["reason"] == "no-draft"
    assert not proposals.exists()


def test_extract_dedups_against_existing_lesson(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    proposals = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(le, "AUDIT_LOG", tmp_path / "audit.log")
    # Existing lesson trigger overlaps the pattern stem.
    monkeypatch.setattr(le, "existing_triggers",
                        lambda *a, **k: ["nudged idle agent to resume work now"])
    _write_ledger(ledger, [
        _entry("stranded", "nudged idle agent to resume work now", epoch=1000 + i)
        for i in range(3)
    ])
    stub = StubLLM()
    r = le.extract(llm=stub, ledger_path=ledger, proposals_path=proposals, now=2000,
                   curator=tmp_path / "curator.py")
    assert r["n_proposed"] == 0
    assert stub.calls == 0, "must not call the LLM for a deduped candidate"


# ─── _extract_json (pure) ────────────────────────────────────────────────────

def test_extract_json_handles_code_fence():
    raw = "```json\n{\"trigger\": \"t\", \"rule\": \"r\"}\n```"
    assert le._extract_json(raw) == {"trigger": "t", "rule": "r"}


def test_extract_json_handles_surrounding_prose():
    raw = "Sure! Here is the lesson:\n{\"trigger\": \"t\", \"rule\": \"r\"} — hope that helps"
    assert le._extract_json(raw) == {"trigger": "t", "rule": "r"}


def test_extract_json_returns_none_on_junk():
    assert le._extract_json("no json at all here") is None
