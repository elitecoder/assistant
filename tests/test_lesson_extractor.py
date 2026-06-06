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
import os
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

    r1 = le.extract(ledger_only=True, llm=stub, ledger_path=ledger,
                    proposals_path=proposals,
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
    r2 = le.extract(ledger_only=True, llm=stub, ledger_path=ledger,
                    proposals_path=proposals,
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
    r = le.extract(dry_run=True, ledger_only=True, llm=stub, ledger_path=ledger,
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
    r = le.extract(ledger_only=True, llm=lambda prompt: "sorry I cannot do that",
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
    r = le.extract(ledger_only=True, llm=stub, ledger_path=ledger,
                   proposals_path=proposals, now=2000,
                   curator=tmp_path / "curator.py")
    assert r["n_proposed"] == 0
    assert stub.calls == 0, "must not call the LLM for a deduped candidate"


# ─── TranscriptScanner ───────────────────────────────────────────────────────

def _turn(role: str, text, **extra) -> str:
    """One transcript JSONL line. `text` may be a str or a list of content
    blocks (as Claude Code actually writes assistant turns)."""
    obj = {"type": role, "message": {"role": role, "content": text}}
    obj.update(extra)
    return json.dumps(obj)


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


_scan_call = [0]


def _scan_dir(tmp_path: Path, files: dict[str, list[str]], *,
              existing=None, now=None) -> list[dict]:
    """Write {filename: [jsonl-lines]} into a fresh fake projects root and scan
    it. Each call gets its own root so repeated calls in one test don't pool."""
    _scan_call[0] += 1
    root = tmp_path / f"root{_scan_call[0]}" / "-Users-mukuls-dev-assistant"
    root.mkdir(parents=True, exist_ok=True)
    for name, lines in files.items():
        _write_transcript(root / name, lines)
    sc = le.TranscriptScanner(roots=[root], existing=existing or [], now=now)
    return sc.scan()


def test_transcript_scanner_correction_signal(tmp_path):
    cands = _scan_dir(tmp_path, {"a.jsonl": [
        _turn("assistant", [{"type": "text", "text": "I'll force-push the branch."}]),
        _turn("user", "No, don't force-push. Never do that without asking."),
    ]})
    corr = [c for c in cands if c["signal"] == "correction"]
    assert len(corr) == 1
    assert corr[0]["type"] == "transcript"
    assert "force-push" in corr[0]["assistant_context"]
    assert corr[0]["source_file"] == "a.jsonl"
    assert "force-push" in corr[0]["user_signal"]


def test_transcript_scanner_confirmation_signal(tmp_path):
    cands = _scan_dir(tmp_path, {"b.jsonl": [
        _turn("assistant", [{"type": "text", "text": "I drafted the Slack message for you to send."}]),
        _turn("user", "Yes exactly, perfect. Keep doing that."),
    ]})
    conf = [c for c in cands if c["signal"] == "confirmation"]
    assert len(conf) == 1
    assert "drafted the Slack message" in conf[0]["assistant_context"]


def test_transcript_scanner_dedup_existing_lesson(tmp_path):
    # An existing lesson already covers the correction → it must be dropped.
    text = "Don't force-push the branch without asking first"
    cands = _scan_dir(tmp_path, {"c.jsonl": [
        _turn("assistant", [{"type": "text", "text": "Force-pushing now."}]),
        _turn("user", "Don't force-push the branch without asking first."),
    ]}, existing=["Don't force-push the branch without asking first, ever"])
    # The signal stem (first 10 words of the user turn) overlaps the existing
    # trigger stem, so no correction candidate survives.
    assert [c for c in cands if c["signal"] == "correction"] == []


def test_transcript_scanner_file_size_cap(tmp_path, monkeypatch):
    root = tmp_path / "-Users-mukuls-dev-assistant"
    root.mkdir(parents=True)
    big = root / "big.jsonl"
    # A valid correction, but the file is over the 5MB cap → never read.
    body = [
        _turn("assistant", [{"type": "text", "text": "doing it"}]),
        _turn("user", "No, that's wrong, stop."),
    ]
    pad = "x" * (le.TranscriptScanner.MAX_FILE_BYTES + 1)
    big.write_text("\n".join(body) + "\n" + json.dumps({"pad": pad}) + "\n")
    assert big.stat().st_size > le.TranscriptScanner.MAX_FILE_BYTES
    sc = le.TranscriptScanner(roots=[root], existing=[])
    assert sc._gather_files() == [], "file over the size cap must be excluded"
    assert sc.scan() == []


def test_transcript_scanner_mtime_filter(tmp_path):
    root = tmp_path / "-Users-mukuls-dev-assistant"
    root.mkdir(parents=True)
    old = root / "old.jsonl"
    _write_transcript(old, [
        _turn("assistant", [{"type": "text", "text": "doing it"}]),
        _turn("user", "No, that's wrong, stop doing that."),
    ])
    # Backdate well beyond the 90-day window.
    window = le.TranscriptScanner.MTIME_WINDOW_DAYS
    now = 2_000_000_000.0
    old_mtime = now - (window + 5) * 86400
    os.utime(old, (old_mtime, old_mtime))
    sc = le.TranscriptScanner(roots=[root], existing=[], now=now)
    assert sc._gather_files() == [], "file older than the mtime window is skipped"


def test_transcript_scanner_skips_ismeta_and_sidechain(tmp_path):
    # isMeta = slash-command skill body; isSidechain = subagent prompt. Neither
    # is the operator, even though both contain correction-shaped words.
    cands = _scan_dir(tmp_path, {"d.jsonl": [
        _turn("user", "Base directory for this skill: stop, don't, wrong", isMeta=True),
        _turn("user", "No don't do that, you are wrong", isSidechain=True),
        _turn("assistant", [{"type": "text", "text": "real reply"}]),
        _turn("user", "Yeah! love it, perfect."),
    ]})
    # Only the genuine confirmation survives; the meta/sidechain corrections drop.
    assert [c["signal"] for c in cands] == ["confirmation"]


def test_transcript_scanner_strips_telegram_prefix(tmp_path):
    cands = _scan_dir(tmp_path, {"e.jsonl": [
        _turn("assistant", [{"type": "text", "text": "I closed the workspace."}]),
        _turn("user", "[telegram chat_id=8188685666 msg_id=12 reply_to=None] "
                      "No, don't close the workspace on cleanup."),
    ]})
    corr = [c for c in cands if c["signal"] == "correction"]
    assert len(corr) == 1
    assert not corr[0]["user_signal"].startswith("[telegram")


def test_transcript_scanner_ignores_tool_result_user_turns(tmp_path):
    # A user turn whose content is a tool_result block is not human text.
    cands = _scan_dir(tmp_path, {"f.jsonl": [
        _turn("assistant", [{"type": "text", "text": "ran the command"}]),
        _turn("user", [{"type": "tool_result", "content": "no such file; stop"}]),
    ]})
    assert cands == []


def test_transcript_scanner_recurring_question_across_sessions(tmp_path):
    q = "what is the deploy status?"
    files = {f"s{i}.jsonl": [_turn("user", q)] for i in range(3)}
    cands = _scan_dir(tmp_path, files)
    rq = [c for c in cands if c["signal"] == "recurring_question"]
    assert len(rq) == 1
    assert rq[0]["count"] == 3
    # Two sessions is below the threshold → no candidate.
    cands2 = _scan_dir(tmp_path, {f"t{i}.jsonl": [_turn("user", q)] for i in range(2)})
    assert [c for c in cands2 if c["signal"] == "recurring_question"] == []


def test_transcript_scanner_malformed_jsonl_does_not_crash(tmp_path):
    cands = _scan_dir(tmp_path, {"g.jsonl": [
        "{not valid json",
        _turn("assistant", [{"type": "text", "text": "ok"}]),
        "}}}garbage",
        _turn("user", "No, that's wrong, stop."),
        "",
    ]})
    assert len(cands) == 1 and cands[0]["signal"] == "correction"


def test_transcript_build_prompt_branches_by_signal():
    corr = {"type": "transcript", "signal": "correction", "count": 2,
            "assistant_context": "force-pushed", "user_signal": "no don't"}
    p = le.build_draft_prompt(corr)
    assert "pushed back" in p and "force-pushed" in p
    conf = {**corr, "signal": "confirmation"}
    assert "approved" in le.build_draft_prompt(conf)
    q = {**corr, "signal": "recurring_question"}
    assert "SAME question" in le.build_draft_prompt(q)


def test_draft_lesson_honors_skip_and_coerces_scope():
    corr = {"type": "transcript", "signal": "correction", "count": 1,
            "assistant_context": "x", "user_signal": "y"}
    # {"skip": true} → no draft.
    assert le.draft_lesson(corr, llm=lambda p: '{"skip": true}') is None
    # An invalid scope for the chosen target gets coerced to that target's default.
    draft = le.draft_lesson(corr, llm=lambda p: json.dumps(
        {"trigger": "t", "rule": "r", "target": "claude", "scope": "bogus"}))
    assert draft["target"] == "claude" and draft["scope"] == "global"
    # An unknown target falls back to assistant + general.
    draft2 = le.draft_lesson(corr, llm=lambda p: json.dumps(
        {"trigger": "t", "rule": "r", "target": "nope", "scope": "nope"}))
    assert draft2["target"] == "assistant" and draft2["scope"] == "general"


def test_extract_ledger_only_skips_transcripts(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    proposals = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(le, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(le, "existing_triggers", lambda *a, **k: [])
    _write_ledger(ledger, [])

    class BoomScanner:
        def __init__(self, *a, **k):
            raise AssertionError("scanner must not be constructed with --ledger-only")

    r = le.extract(ledger_only=True, llm=StubLLM(), ledger_path=ledger,
                   proposals_path=proposals, now=2000,
                   curator=tmp_path / "curator.py",
                   scanner_factory=lambda triggers: BoomScanner())
    assert r["ledger_only"] is True
    assert r["n_transcript_candidates"] == 0


def test_extract_runs_transcript_pass_through_pipeline(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    proposals = tmp_path / "proposals.jsonl"
    monkeypatch.setattr(le, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(le, "existing_triggers", lambda *a, **k: [])
    _write_ledger(ledger, [])  # no ledger candidates — transcript-only

    class FakeScanner:
        def __init__(self, *a, **k):
            pass

        def scan(self):
            return [{
                "type": "transcript", "signal": "correction",
                "source_file": "x.jsonl", "assistant_context": "did a thing",
                "user_signal": "no don't", "stem": "no dont do that thing again",
                "kind": "transcript:correction", "count": 4,
            }]

    stub = StubLLM()
    r = le.extract(llm=stub, ledger_path=ledger, proposals_path=proposals,
                   now=2000, curator=tmp_path / "curator.py",
                   tg_send=tmp_path / "tg-send.py",
                   scanner_factory=lambda triggers: FakeScanner())
    assert r["n_transcript_candidates"] == 1
    assert r["n_proposed"] == 1
    line = json.loads(proposals.read_text().splitlines()[0])
    assert line["source"] == "extractor-transcript"
    assert line["pattern_signal"] == "correction"
    assert line["source_file"] == "x.jsonl"


# ─── _extract_json (pure) ────────────────────────────────────────────────────

def test_extract_json_handles_code_fence():
    raw = "```json\n{\"trigger\": \"t\", \"rule\": \"r\"}\n```"
    assert le._extract_json(raw) == {"trigger": "t", "rule": "r"}


def test_extract_json_handles_surrounding_prose():
    raw = "Sure! Here is the lesson:\n{\"trigger\": \"t\", \"rule\": \"r\"} — hope that helps"
    assert le._extract_json(raw) == {"trigger": "t", "rule": "r"}


def test_extract_json_returns_none_on_junk():
    assert le._extract_json("no json at all here") is None
