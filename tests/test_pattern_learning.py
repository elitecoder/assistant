"""Tests for the cmux-watcher learning loops in bin/lesson-extractor.py:

  - notification-noise feedback: a transcript correction about pinging routes a
    feedback=noise call to the implicated pattern.
  - new-pattern discovery: recurring needs_user terminal phrases that no current
    pattern catches become pattern proposals (idempotent across runs).

Pure logic is exercised directly; the pattern-feedback CLI call is captured via
an injected runner so nothing shells out."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, fname: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(REPO / "bin" / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


le = _load("lesson_extractor_pl", "lesson-extractor.py")


# ─── notification-noise classification ────────────────────────────────────────

def test_is_notification_correction_positive():
    assert le.is_notification_correction("stop pinging me about that")
    assert le.is_notification_correction("that ping wasn't worth it")
    assert le.is_notification_correction("too many alerts, quiet down")


def test_is_notification_correction_negative():
    # Mentions notification but no negative sentiment → not a noise correction.
    assert not le.is_notification_correction("the ping arrived, thanks")
    # Negative sentiment but not about notifications → not ours.
    assert not le.is_notification_correction("stop editing that file")


def test_match_pattern_for_correction():
    patterns = [
        {"id": "tests-pass"},
        {"id": "ci-green"},
        {"id": "pr-opened"},
    ]
    # "tests pass" → both words present.
    assert le.match_pattern_for_correction(
        "stop pinging me when tests pass", patterns) == "tests-pass"
    # No pattern clearly implicated → None (we never guess).
    assert le.match_pattern_for_correction(
        "stop pinging me about random stuff", patterns) is None


def test_apply_notification_feedback_routes_noise():
    with TemporaryDirectory() as d:
        bank = Path(d) / "pattern_bank.json"
        bank.write_text(json.dumps({"version": 1, "patterns": [
            {"id": "tests-pass", "regex": r"\d+ tests pass", "signal": "work_complete",
             "priority": "low"},
        ]}))
        calls = []

        def runner(argv):
            calls.append(argv)
            return (0, "", "")

        candidates = [
            {"type": "transcript", "signal": "correction",
             "user_signal": "please stop pinging me every time tests pass"},
            {"type": "transcript", "signal": "correction",
             "user_signal": "no, fix the bug differently"},  # not notification
        ]
        acted = le.apply_notification_feedback(
            candidates, bank_path=bank, runner=runner)
        assert len(acted) == 1
        assert acted[0]["pattern_id"] == "tests-pass"
        # The feedback CLI was invoked with feedback=noise for that pattern.
        assert any("--feedback" in a and "noise" in a and "tests-pass" in a
                   for a in calls)


def test_apply_notification_feedback_dry_run_no_call():
    with TemporaryDirectory() as d:
        bank = Path(d) / "pattern_bank.json"
        bank.write_text(json.dumps({"version": 1, "patterns": [
            {"id": "ci-green", "regex": "ci green", "signal": "work_complete",
             "priority": "medium"},
        ]}))
        calls = []
        candidates = [{"type": "transcript", "signal": "correction",
                       "user_signal": "stop pinging me about ci green builds"}]
        acted = le.apply_notification_feedback(
            candidates, dry_run=True, bank_path=bank,
            runner=lambda a: (calls.append(a) or (0, "", "")))
        assert acted and acted[0]["dry_run"] is True
        assert calls == [], "dry-run must not invoke the feedback CLI"


# ─── new-pattern discovery ────────────────────────────────────────────────────

def _ledger(path: Path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_discover_patterns_finds_recurring_uncovered_phrase():
    with TemporaryDirectory() as d:
        d = Path(d)
        bank = d / "pattern_bank.json"
        # A bank that does NOT cover "model registry sync required".
        bank.write_text(json.dumps({"version": 1, "patterns": [
            {"id": "pr-opened", "regex": r"PR #\d+ opened", "signal": "work_complete",
             "priority": "high"},
        ]}))
        ledger = d / "ledger.jsonl"
        proposals = d / "proposals.jsonl"
        rows = [
            {"kind": "emit-card", "outcome": "verified", "epoch": 100,
             "evidence": "Model registry sync required before continue"},
            {"kind": "emit-card", "outcome": "verified", "epoch": 200,
             "evidence": "Model registry sync required before continue"},
            {"kind": "noop", "outcome": "verified", "epoch": 300,
             "evidence": "nothing to see"},
        ]
        _ledger(ledger, rows)
        cands = le.discover_patterns(
            ledger_path=ledger, bank_path=bank, proposals_path=proposals,
            min_occurrences=2, now=1000)
        assert cands, "a phrase recurring in 2 needs_user entries should surface"
        assert cands[0]["count"] >= 2
        assert "registry sync" in cands[0]["sample"].lower()


def test_discover_skips_phrases_covered_by_bank():
    with TemporaryDirectory() as d:
        d = Path(d)
        bank = d / "pattern_bank.json"
        bank.write_text(json.dumps({"version": 1, "patterns": [
            {"id": "awaiting", "regex": "awaiting your review", "signal": "needs_input",
             "priority": "high"},
        ]}))
        ledger = d / "ledger.jsonl"
        rows = [
            {"kind": "emit-card", "outcome": "verified", "epoch": 100,
             "evidence": "This PR is awaiting your review now"},
            {"kind": "emit-card", "outcome": "verified", "epoch": 200,
             "evidence": "This PR is awaiting your review now"},
        ]
        _ledger(ledger, rows)
        cands = le.discover_patterns(
            ledger_path=ledger, bank_path=bank, proposals_path=d / "p.jsonl",
            min_occurrences=2, now=1000)
        # Already covered by the "awaiting" pattern → no candidate.
        assert cands == []


def test_discover_idempotent_via_pending_proposals():
    with TemporaryDirectory() as d:
        d = Path(d)
        bank = d / "pattern_bank.json"
        bank.write_text(json.dumps({"version": 1, "patterns": []}))
        ledger = d / "ledger.jsonl"
        proposals = d / "proposals.jsonl"
        rows = [
            {"kind": "needs_user", "outcome": "verified", "epoch": 100,
             "evidence": "Bedrock token expired please refresh"},
            {"kind": "needs_user", "outcome": "verified", "epoch": 200,
             "evidence": "Bedrock token expired please refresh"},
        ]
        _ledger(ledger, rows)
        # First discovery + write a proposal.
        res1 = le.run_discovery(ledger_path=ledger, bank_path=bank,
                                proposals_path=proposals,
                                now=1000)
        assert res1["n_proposed"] == 1
        # Second run sees the pending pattern proposal and proposes nothing new.
        res2 = le.run_discovery(ledger_path=ledger, bank_path=bank,
                                proposals_path=proposals,
                                now=1000)
        assert res2["n_proposed"] == 0, "discovery must be idempotent"


def test_write_pattern_proposal_shape():
    with TemporaryDirectory() as d:
        proposals = Path(d) / "proposals.jsonl"
        cand = {"type": "pattern", "stem": "model registry sync required",
                "sample": "Model registry sync required", "count": 3}
        pid = le.write_pattern_proposal(cand, proposals)
        line = proposals.read_text().strip()
        obj = json.loads(line)
        assert obj["type"] == "pattern"
        assert obj["status"] == "pending"
        assert obj["id"] == pid
        assert obj["proposed_pattern"]["signal"] == "needs_input"
        # The proposed regex is a literal-escaped match of the sample.
        import re
        assert re.search(obj["proposed_pattern"]["regex"],
                         "Model registry sync required now", re.I)
