"""Tests for comms_session.py — the PURE logic (registry, transcript, should_clear).
The cmux I/O functions are marked `pragma: no cover` and validated live, not here."""
from __future__ import annotations

import json
import os
from pathlib import Path

import agent_session as ag
import comms_lib as cl
import comms_session as cs
import pytest


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    return cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})


# ─── session registry ───────────────────────────────────────────────────────

def test_session_registry_roundtrip(paths: cl.Paths):
    assert cs.read_session(paths) is None
    cs.write_session(paths, "workspace:5", "surface:3", "/cwd", "/t.jsonl",
                     clock=lambda: 1700)
    sess = cs.read_session(paths)
    assert sess["ws_ref"] == "workspace:5"
    assert sess["surface_ref"] == "surface:3"
    assert sess["spawned_ts"] == 1700
    cs.clear_session_registry(paths)
    assert cs.read_session(paths) is None


def test_read_session_bad_json(paths: cl.Paths):
    cs.session_registry_path(paths).parent.mkdir(parents=True, exist_ok=True)
    cs.session_registry_path(paths).write_text("{not json")
    assert cs.read_session(paths) is None


# ─── transcript logic ───────────────────────────────────────────────────────

def test_last_assistant_text_list_and_str(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(
        json.dumps({"type": "assistant", "message": {"content": "plain"}}) + "\n"
        + json.dumps({"type": "user", "message": {"content": "ignored"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]}}) + "\n")
    assert cs.last_assistant_text(t) == "hello world"


def test_last_assistant_text_none_when_no_assistant(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    assert cs.last_assistant_text(t) is None


def test_transcript_line_count(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text("a\n\nb\n  \nc\n")
    assert cs.transcript_line_count(t) == 3
    assert cs.transcript_line_count(tmp_path / "missing.jsonl") == 0


def test_should_clear_uses_threshold(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"usage": {"input_tokens": 600_000}}}) + "\n")
    assert cs.should_clear(t, threshold=0.5) is True
    assert cs.should_clear(t, threshold=0.7) is False


def test_should_clear_no_usage_is_false(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user"}) + "\n")
    assert cs.should_clear(t) is False


def test_project_dir_for_cwd_slug():
    cwd = os.path.realpath("/tmp")
    d = cs.project_dir_for_cwd("/tmp")
    assert d.name == cwd.replace("/", "-")


def test_newest_transcript_none_for_missing(tmp_path: Path):
    assert cs.newest_transcript(str(tmp_path / "nowhere")) is None


# ─── droid schema parity (G2 read) ──────────────────────────────────────────

def test_last_assistant_text_droid_schema(tmp_path: Path):
    """A DROID-schema transcript (type=='message', role at message.role) yields
    the droid assistant turn's text — the claude type=='assistant' filter would
    have skipped every row and returned None."""
    t = tmp_path / "d.jsonl"
    t.write_text(
        json.dumps({"type": "session_start", "message": {}}) + "\n"
        + json.dumps({"type": "message", "message": {"role": "user",
                     "content": "ping"}}) + "\n"
        + json.dumps({"type": "message", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "droid reply"}]}}) + "\n")
    assert cs.last_assistant_text(t) == "droid reply"


def test_project_dir_for_cwd_droid_root():
    """The droid variant resolves under ~/.factory/sessions, not ~/.claude."""
    claude_dir = cs.project_dir_for_cwd("/tmp", ag.CLAUDE)
    droid_dir = cs.project_dir_for_cwd("/tmp", ag.DROID)
    assert ".claude/projects" in str(claude_dir)
    assert ".factory/sessions" in str(droid_dir)
    assert claude_dir.name == droid_dir.name  # shared slug


# ─── should_clear provider-aware (G3) ───────────────────────────────────────

def test_should_clear_droid_uses_byte_size(tmp_path: Path):
    """Droid transcripts have no usage block; the claude path would peg False
    forever. The droid branch trips on transcript byte size instead."""
    t = tmp_path / "d.jsonl"
    t.write_bytes(b"x" * 100)
    assert cs.should_clear(t, agent=ag.DROID, droid_clear_bytes=50) is True
    assert cs.should_clear(t, agent=ag.DROID, droid_clear_bytes=1000) is False


def test_should_clear_droid_no_usage_block_would_stick_false_on_claude(tmp_path: Path):
    """The exact regression: a droid transcript (no usage block) that HAS grown
    large. The claude usage-fraction path returns False (None tokens); the
    droid byte-size path correctly returns True."""
    t = tmp_path / "d.jsonl"
    t.write_text(
        (json.dumps({"type": "message",
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "hi"}]}}) + "\n") * 200)
    assert cs.should_clear(t, agent=ag.CLAUDE) is False  # usage path sticks False
    assert cs.should_clear(t, agent=ag.DROID, droid_clear_bytes=100) is True


def test_should_clear_droid_missing_transcript_false(tmp_path: Path):
    assert cs.should_clear(tmp_path / "nope.jsonl", agent=ag.DROID,
                           droid_clear_bytes=1) is False


# ─── agent persisted into session.json (G2) ─────────────────────────────────

def test_write_session_persists_agent(paths: cl.Paths):
    cs.write_session(paths, "workspace:5", "surface:3", "/cwd", "/t.jsonl",
                     agent=ag.DROID, clock=lambda: 1700)
    assert cs.read_session(paths)["agent"] == ag.DROID


def test_write_session_preserves_agent_on_refresh(paths: cl.Paths):
    """The comms-listen transcript-refresh path calls write_session WITHOUT an
    agent (agent=None). It must reuse the persisted provider, not silently reset
    a droid session to claude."""
    cs.write_session(paths, "workspace:5", "surface:3", "/cwd", "/t.jsonl",
                     agent=ag.DROID)
    cs.write_session(paths, "workspace:5", "surface:3", "/cwd", "/t2.jsonl")
    sess = cs.read_session(paths)
    assert sess["agent"] == ag.DROID
    assert sess["transcript_path"] == "/t2.jsonl"


def test_write_session_defaults_agent_claude(paths: cl.Paths):
    """No persisted record and no explicit agent → the coexistence default."""
    cs.write_session(paths, "workspace:5", "surface:3", "/cwd", "/t.jsonl")
    assert cs.read_session(paths)["agent"] == ag.CLAUDE


# ─── claude path unchanged (byte-identical target) ──────────────────────────

def test_should_clear_claude_default_agent_matches_usage_path(tmp_path: Path):
    """Default agent is claude and the usage-block behavior is preserved."""
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"usage": {"input_tokens": 600_000}}}) + "\n")
    assert cs.should_clear(t) is True
    assert cs.should_clear(t, agent=ag.CLAUDE) is True
    assert cs.should_clear(t, threshold=0.7) is False


# ─── clear_session provider-aware branch + return contract (integration) ─────
#
# clear_session is `pragma: no cover` for its cmux I/O, but the BRANCH SELECTION
# (which provider path runs) and the return-value contract are pure decisions —
# tested here by stubbing every cmux-touching helper with fakes that record.


def _stub_cmux(monkeypatch):
    """Neutralize every cmux/sleep touchpoint clear_session can reach and return
    recorders. No real cmux RPC, no wall-clock sleeps."""
    monkeypatch.setattr(cs.time, "sleep", lambda *a, **k: None)
    rpc_calls: list = []
    monkeypatch.setattr(cs, "_cmux_rpc",
                        lambda p, method, params, timeout=15: rpc_calls.append((method, params)))
    monkeypatch.setattr(cs, "_surface_read_text", lambda *a, **k: "Welcome back")
    feeds: list = []
    monkeypatch.setattr(cs, "feed", lambda p, s, text: feeds.append(text))
    monkeypatch.setattr(cs, "newest_transcript", lambda cwd, agent: "/new-t.jsonl")
    calls: dict = {}

    def fake_close(p, ws, log=lambda m: None):
        calls["close"] = ws
    monkeypatch.setattr(cs, "close_own_workspace", fake_close)

    respawned = {"ws_ref": "workspace:99", "surface_ref": "surface:9",
                 "cwd": "/cwd", "transcript_path": "/respawn.jsonl", "agent": ag.DROID}

    def fake_spawn(p, boot_prompt, log=lambda m: None, agent=None):
        calls["spawn_agent"] = agent
        return respawned
    monkeypatch.setattr(cs, "spawn_session", fake_spawn)
    return rpc_calls, feeds, calls, respawned


def test_clear_session_droid_respawns_not_clear(paths: cl.Paths, monkeypatch):
    """droid branch: close the warm workspace + spawn a fresh one, send NO
    /clear, and return the respawned record (lossless respawn)."""
    rpc_calls, feeds, calls, respawned = _stub_cmux(monkeypatch)
    sess = {"ws_ref": "workspace:5", "surface_ref": "surface:3", "cwd": "/cwd"}
    out = cs.clear_session(paths, sess, Path("/boot.md"), agent=ag.DROID)
    assert calls["close"] == "workspace:5"
    assert calls["spawn_agent"] == ag.DROID
    assert out == respawned
    assert rpc_calls == [], "droid respawn must not send a /clear"


def test_clear_session_claude_clears_in_place_and_returns_refreshed(paths: cl.Paths, monkeypatch):
    """claude branch: send the /clear sequence, NEVER spawn a new session, and
    return the refreshed registry record carrying the new transcript path."""
    rpc_calls, feeds, calls, _ = _stub_cmux(monkeypatch)
    sess = {"ws_ref": "workspace:5", "surface_ref": "surface:3", "cwd": "/cwd"}
    out = cs.clear_session(paths, sess, Path("/boot.md"), agent=ag.CLAUDE)
    assert any(m == "surface.send_text" and params.get("text") == "/clear"
               for m, params in rpc_calls), "claude branch must send /clear"
    assert "spawn_agent" not in calls, "claude branch must not spawn a new session"
    assert out["transcript_path"] == "/new-t.jsonl"
    assert out["ws_ref"] == "workspace:5"
    assert out["agent"] == ag.CLAUDE


# ─── instance-scoped warm-workspace reconcile (reconcile bug fix) ─────────────

def test_spawned_ledger_roundtrip(paths: cl.Paths):
    assert cs.read_spawned_refs(paths) == []
    cs.record_spawned_ref(paths, "workspace:10")
    cs.record_spawned_ref(paths, "workspace:11")
    assert cs.read_spawned_refs(paths) == ["workspace:10", "workspace:11"]
    # Idempotent — a repeat ref is not duplicated.
    cs.record_spawned_ref(paths, "workspace:10")
    assert cs.read_spawned_refs(paths) == ["workspace:10", "workspace:11"]


def test_read_spawned_refs_bad_json(paths: cl.Paths):
    p = cs.spawned_ledger_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert cs.read_spawned_refs(paths) == []


def test_refs_to_reconcile_excludes_keep_and_dedupes():
    # Every spawned ref except keep, first-seen order, de-duplicated.
    got = cs.refs_to_reconcile(
        ["workspace:1", "workspace:2", "workspace:1", "workspace:3"],
        keep="workspace:2")
    assert got == ["workspace:1", "workspace:3"]


def test_refs_to_reconcile_keep_none_closes_all():
    assert cs.refs_to_reconcile(["workspace:1", "workspace:2"], keep=None) == \
        ["workspace:1", "workspace:2"]


def test_refs_to_reconcile_empty_ledger():
    assert cs.refs_to_reconcile([], keep="workspace:9") == []


def test_reconcile_is_instance_scoped_never_touches_other_instance(tmp_path, monkeypatch):
    """The bug: reconcile closed EVERY warm-titled workspace machine-wide, so a
    second comms instance (distinct COMMS_HOME) or a live-validation spawn closed
    the production instance's warm session. Fix: reconcile only closes refs in
    THIS instance's own spawned-workspaces ledger."""
    home_a = tmp_path / "a"; (home_a / ".assistant").mkdir(parents=True)
    home_b = tmp_path / "b"; (home_b / ".assistant").mkdir(parents=True)
    paths_a = cl.Paths.from_env({"HOME": str(home_a), "COMMS_HOME": str(home_a)})
    paths_b = cl.Paths.from_env({"HOME": str(home_b), "COMMS_HOME": str(home_b)})

    # Instance A spawned ws:1; instance B spawned ws:2 (the production session).
    cs.record_spawned_ref(paths_a, "workspace:1")
    cs.record_spawned_ref(paths_b, "workspace:2")

    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda paths, ws, log=lambda m: None: closed.append(ws))

    # Instance A reconciles after respawning ws:3 — it must close only its OWN
    # orphan (ws:1) and NEVER B's production ws:2.
    cs.record_spawned_ref(paths_a, "workspace:3")
    cs.reconcile_warm_workspaces(paths_a, keep="workspace:3")

    assert "workspace:2" not in closed, "reconcile must not touch another instance's session"
    assert closed == ["workspace:1"]
    # B's ledger is untouched; A's ledger now holds only the survivor.
    assert cs.read_spawned_refs(paths_b) == ["workspace:2"]
    assert cs.read_spawned_refs(paths_a) == ["workspace:3"]


# ─── machine-wide orphan sweep (complements instance-scoped reconcile) ──────

def test_orphan_warm_refs_finds_untracked_warm_workspaces():
    """Orphans are warm-titled workspaces NOT in the ledger and NOT keep —
    leftovers from a prior incarnation whose ledger was lost/reset."""
    all_warm = ["workspace:48", "workspace:47", "workspace:20"]
    ledger = ["workspace:48"]
    got = cs.orphan_warm_refs(all_warm, ledger, keep="workspace:48")
    assert got == ["workspace:47", "workspace:20"]


def test_orphan_warm_refs_excludes_ledger_refs_even_if_not_keep():
    """A ledger-tracked ref is NOT an orphan — reconcile owns it."""
    all_warm = ["workspace:1", "workspace:2", "workspace:3"]
    ledger = ["workspace:1", "workspace:2"]
    got = cs.orphan_warm_refs(all_warm, ledger, keep="workspace:3")
    assert got == []  # ws:1 and ws:2 are in the ledger, ws:3 is keep


def test_orphan_warm_refs_dedupes():
    got = cs.orphan_warm_refs(
        ["workspace:5", "workspace:5", "workspace:6"], [], keep="workspace:7")
    assert got == ["workspace:5", "workspace:6"]


def test_orphan_warm_refs_empty_when_all_tracked():
    assert cs.orphan_warm_refs(["workspace:1"], ["workspace:1"], keep="workspace:1") == []


def test_orphan_warm_refs_keep_none_orphans_all_untracked():
    """keep=None means everything untracked is an orphan."""
    got = cs.orphan_warm_refs(["workspace:1", "workspace:2"], [], keep=None)
    assert got == ["workspace:1", "workspace:2"]


def test_sweep_orphan_warm_workspaces_closes_stale_and_skips_current(tmp_path, monkeypatch):
    """Integration: sweep closes machine-wide warm workspaces not in the ledger
    and not keep, using the title-guarded close_own_workspace."""
    home = tmp_path / "home"; (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    # Current instance only tracks workspace:48 in its ledger.
    cs.record_spawned_ref(paths, "workspace:48")

    # Machine-wide scan finds 3 warm workspaces (47 and 20 are orphans).
    monkeypatch.setattr(cs, "list_warm_workspaces",
                        lambda p: ["workspace:48", "workspace:47", "workspace:20"])
    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))

    orphans = cs.sweep_orphan_warm_workspaces(paths, keep="workspace:48", log=lambda m: None)

    assert orphans == ["workspace:47", "workspace:20"]
    assert closed == ["workspace:47", "workspace:20"]
    assert "workspace:48" not in closed, "sweep must never close the current session"
