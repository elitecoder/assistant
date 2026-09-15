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


# ─── warm launch backend selection ──────────────────────────────────────────
# Regression coverage for the 2026-09-05 bug: comms_session hardcoded a
# Bedrock-shaped model id (`us.anthropic.claude-sonnet-4-6[1m]`) regardless of
# which backend was actually live, so a non-Bedrock alias got handed an id its
# own backend would reject. The fix derives both the model id AND an explicit
# CLAUDE_CODE_USE_BEDROCK=<0|1> launch-command prefix from the SAME
# model_tiers.provider() call, so the launched session can never disagree
# with its own backend the way an inherited/assumed env could.
#
# These exercise the REAL resolution path (env -> model_tiers.provider() ->
# model_tiers.model_for()), not a mock of the values under test — an earlier
# version of these tests monkeypatched the (now-removed) WARM_MODEL/
# WARM_BACKEND constants directly, which passed even with the fix fully
# reverted (2026-09-05 brutal-review finding: mutation-proven vacuous).

@pytest.fixture(autouse=True)
def _isolated_backend_env(monkeypatch):
    # CLAUDE_CODE_USE_BEDROCK env (when set) beats ~/.zprofile, so setting it
    # explicitly in every test isolates from whatever backend this dev box
    # currently has toggled — no test should depend on that external state.
    monkeypatch.delenv("COMMS_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)


def test_warm_launch_declares_bedrock_backend_and_keeps_1m_context(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    cmd = cs._warm_launch(ag.CLAUDE)
    assert cmd.startswith("CLAUDE_CODE_USE_BEDROCK=1 ")
    assert "us.anthropic.claude-sonnet-4-6[1m]" in cmd


def test_warm_launch_declares_non_bedrock_backend_and_still_gets_1m_context(monkeypatch):
    # Regression for the review's CRITICAL finding: model_tiers used to add
    # [1m] ONLY on Bedrock, so the non-Bedrock path silently lost 1M context
    # (breaking should_clear's whole 50%-of-1M-token design) the moment this
    # file started routing through model_tiers instead of a Bedrock-shaped
    # literal. Direct Anthropic DOES accept [1m] (verified live against the
    # operator's own non-Bedrock ~/.zprofile alias) — this must stay present.
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    cmd = cs._warm_launch(ag.CLAUDE)
    assert cmd.startswith("CLAUDE_CODE_USE_BEDROCK=0 ")
    assert "us.anthropic." not in cmd
    assert "[1m]" in cmd, "non-Bedrock must not silently lose the 1M context window"


def test_warm_launch_pinned_model_skips_the_backend_prefix(monkeypatch):
    # Regression for a footgun the review found: auto-declaring
    # CLAUDE_CODE_USE_BEDROCK from AMBIENT detection while the operator has
    # explicitly pinned COMMS_MODEL to a specific id can contradict the pin
    # (e.g. a Bedrock id pinned on a box whose zprofile currently reads
    # non-Bedrock). An explicit pin means the operator already knows what
    # they're doing — don't second-guess it with an auto-declared flag.
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    monkeypatch.setenv("COMMS_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
    cmd = cs._warm_launch(ag.CLAUDE)
    assert "CLAUDE_CODE_USE_BEDROCK" not in cmd
    assert "us.anthropic.claude-sonnet-4-6[1m]" in cmd


def test_resolve_warm_model_and_backend_respects_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    model, backend, pinned = cs._resolve_warm_model_and_backend()
    assert backend == "bedrock"
    assert model == "us.anthropic.claude-sonnet-4-6[1m]"
    assert pinned is False

    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    model, backend, pinned = cs._resolve_warm_model_and_backend()
    assert backend == "anthropic"
    assert model == "claude-sonnet-4-6[1m]"
    assert pinned is False

    monkeypatch.setenv("COMMS_MODEL", "my-pinned-id")
    model, backend, pinned = cs._resolve_warm_model_and_backend()
    assert (model, pinned) == ("my-pinned-id", True)


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


# ─── parse_ws_ref_from_output (pure) ────────────────────────────────────────

def test_parse_ws_ref_from_output_found_in_stdout():
    assert cs.parse_ws_ref_from_output("created workspace:252\n", "") == "workspace:252"


def test_parse_ws_ref_from_output_found_in_stderr():
    assert cs.parse_ws_ref_from_output("", "Error: workspace:252 timed out") == "workspace:252"


def test_parse_ws_ref_from_output_stdout_wins_over_stderr():
    assert cs.parse_ws_ref_from_output("workspace:10 ok", "workspace:99 err") == "workspace:10"


def test_parse_ws_ref_from_output_not_found():
    assert cs.parse_ws_ref_from_output("", "Error: Command timed out") is None


# ─── untracked_warm_refs (pure) ──────────────────────────────────────────────

def test_untracked_warm_refs_finds_orphan():
    got = cs.untracked_warm_refs(
        warm_refs=["workspace:252", "workspace:254"],
        spawned_refs=["workspace:254"],
        keep="workspace:254",
    )
    assert got == ["workspace:252"]


def test_untracked_warm_refs_skips_keep():
    got = cs.untracked_warm_refs(
        warm_refs=["workspace:254"],
        spawned_refs=[],
        keep="workspace:254",
    )
    assert got == []


def test_untracked_warm_refs_skips_known_spawned():
    got = cs.untracked_warm_refs(
        warm_refs=["workspace:10", "workspace:11"],
        spawned_refs=["workspace:10", "workspace:11"],
        keep="workspace:11",
    )
    assert got == []


def test_untracked_warm_refs_empty_warm():
    assert cs.untracked_warm_refs([], ["workspace:5"], "workspace:5") == []


# ─── reconcile closes title-scanned untracked orphan ────────────────────────

def test_reconcile_closes_untracked_title_scanned_orphan(tmp_path, monkeypatch):
    """Regression: a workspace created by a timed-out spawn is never in the
    spawned ledger.  reconcile_warm_workspaces must find and close it via the
    title-scan pass even though it was never recorded."""
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    # Ledger only knows about ws:254 (the successful spawn).
    cs.record_spawned_ref(paths, "workspace:254")

    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))
    # Title scan returns both ws:252 (orphan) and ws:254 (keep).
    monkeypatch.setattr(cs, "list_warm_workspaces",
                        lambda p: ["workspace:252", "workspace:254"])

    cs.reconcile_warm_workspaces(paths, keep="workspace:254")

    assert "workspace:252" in closed, "untracked orphan must be closed"
    assert "workspace:254" not in closed, "kept session must not be closed"
    assert cs.read_spawned_refs(paths) == ["workspace:254"]


# ─── spawn_session records partial ref on new-workspace timeout ──────────────

def test_spawn_session_records_partial_ref_on_timeout(tmp_path, monkeypatch):
    """When new-workspace times out but prints a workspace ref in its output,
    spawn_session must record that ref in the spawned ledger so the next
    reconcile can close the orphan."""
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    call_count = {"n": 0}

    def fake_run_cmd(cmd, timeout=30):
        call_count["n"] += 1
        if "ping" in cmd:
            return 0, "", ""
        if "new-workspace" in cmd:
            # Simulate cmux printing a workspace ref before timing out.
            return 1, "workspace:252\n", "Error: Command timed out"
        return 1, "", ""

    monkeypatch.setattr(cl, "run_cmd", fake_run_cmd)

    result = cs.spawn_session(paths, Path("/boot.md"))

    assert result is None, "must return None on failure"
    assert cs.read_spawned_refs(paths) == ["workspace:252"], \
        "partial ref must be recorded for next reconcile"


def test_spawn_session_no_partial_ref_when_output_empty(tmp_path, monkeypatch):
    """When new-workspace fails with no workspace ref in output, nothing is
    recorded — the spawned ledger stays empty."""
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    def fake_run_cmd(cmd, timeout=30):
        if "ping" in cmd:
            return 0, "", ""
        if "new-workspace" in cmd:
            return 1, "", "Error: Command timed out"
        return 1, "", ""

    monkeypatch.setattr(cl, "run_cmd", fake_run_cmd)

    result = cs.spawn_session(paths, Path("/boot.md"))

    assert result is None
    assert cs.read_spawned_refs(paths) == []


# ─── failed-spawn cleanup (2026-09-14 leak: ~200 orphaned workspaces killed cmux) ─

def test_spawn_session_closes_workspace_when_never_ready(tmp_path, monkeypatch):
    """The 2026-09-14 leak: new-workspace SUCCEEDS but claude never reaches its
    ready marker (bad boot). spawn_session used to return None leaving the fresh
    workspace alive; the watchdog respawned every tick and ~200 orphans piled up
    until cmux died. spawn_session must now close the workspace it just made.

    Mutation probe: delete the `_abandon_failed_spawn` call on the never-ready
    path and workspace:300 is never closed — this assertion fails."""
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    def fake_run_cmd(cmd, timeout=30):
        if "ping" in cmd:
            return 0, "", ""
        if "new-workspace" in cmd:
            return 0, "workspace:300\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:300\n", ""
        return 0, "", ""

    monkeypatch.setattr(cl, "run_cmd", fake_run_cmd)
    # claude never reaches the ready marker.
    monkeypatch.setattr(cs, "await_ready", lambda **kw: (False, False))

    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))
    # A machine-wide title scan here would close a second comms instance's healthy
    # session; the failure path must NOT scan. Blow up if it does.
    def _no_scan(p):
        raise AssertionError("failure cleanup must not machine-wide title-scan")
    monkeypatch.setattr(cs, "list_warm_workspaces", _no_scan)

    result = cs.spawn_session(paths, Path("/boot.md"))

    assert result is None, "a never-ready spawn must fail"
    assert closed == ["workspace:300"], "only the just-created workspace is closed"


def test_spawn_session_closes_workspace_when_no_surface(tmp_path, monkeypatch):
    """A workspace with no pane surface is unusable but still alive. spawn_session
    must close it rather than leak it. Mutation probe: drop the cleanup call on
    the no-surface path and workspace:301 is never closed."""
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    def fake_run_cmd(cmd, timeout=30):
        if "ping" in cmd:
            return 0, "", ""
        if "new-workspace" in cmd:
            return 0, "workspace:301\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "(no surfaces)\n", ""
        return 0, "", ""

    monkeypatch.setattr(cl, "run_cmd", fake_run_cmd)

    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))

    result = cs.spawn_session(paths, Path("/boot.md"))

    assert result is None
    assert closed == ["workspace:301"], "surface-less workspace must be closed"


def test_spawn_session_closes_partial_ref_on_new_workspace_timeout(tmp_path, monkeypatch):
    """Finding 2: when new-workspace times out (rc!=0) but cmux already created a
    workspace and printed its ref, spawn_session must close that partial ref now.
    Waiting for 'the next reconcile' leaks under a persistent timeout — no
    successful spawn ever comes to run it. The ref is still recorded as a
    belt-and-suspenders fallback if the close itself fails.

    Mutation probe: drop the `_abandon_failed_spawn(paths, partial, ...)` call and
    workspace:252 is never closed."""
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    paths = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})

    def fake_run_cmd(cmd, timeout=30):
        if "ping" in cmd:
            return 0, "", ""
        if "new-workspace" in cmd:
            return 1, "workspace:252\n", "Error: Command timed out"
        return 0, "", ""

    monkeypatch.setattr(cl, "run_cmd", fake_run_cmd)
    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))

    result = cs.spawn_session(paths, Path("/boot.md"))

    assert result is None
    assert closed == ["workspace:252"], "the timed-out partial workspace must be closed"
    assert cs.read_spawned_refs(paths) == ["workspace:252"], \
        "partial ref still recorded as a fallback for the next reconcile"


def test_abandon_failed_spawn_targets_only_the_given_ref(paths: cl.Paths, monkeypatch):
    """Finding 1 regression guard: cleanup closes ONLY the ref it is given and
    never machine-wide title-scans — otherwise a failing instance would close a
    second comms instance's healthy warm session (same title, different
    COMMS_HOME) on every tick. Mutation probe: switch back to
    reconcile_warm_workspaces(keep=None) and the list_warm_workspaces scan fires."""
    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))
    monkeypatch.setattr(cs, "list_warm_workspaces",
                        lambda p: (_ for _ in ()).throw(
                            AssertionError("must not machine-wide scan")))
    cs._abandon_failed_spawn(paths, "workspace:7", log=lambda m: None)
    assert closed == ["workspace:7"]


def test_abandon_failed_spawn_no_ref_is_a_noop(paths: cl.Paths, monkeypatch):
    """The no-ref path (cmux returned success but printed no workspace ref) has
    nothing to target. Cleanup must NOT fall back to a machine-wide scan/close —
    that is the cross-instance hazard. Mutation probe: make it scan and this
    raises."""
    closed: list[str] = []
    monkeypatch.setattr(cs, "close_own_workspace",
                        lambda p, ws, log=lambda m: None: closed.append(ws))
    monkeypatch.setattr(cs, "list_warm_workspaces",
                        lambda p: (_ for _ in ()).throw(
                            AssertionError("must not machine-wide scan")))
    logs: list[str] = []
    cs._abandon_failed_spawn(paths, None, log=logs.append)
    assert closed == []
    assert any("no workspace ref" in m for m in logs)


def test_abandon_failed_spawn_swallows_cleanup_errors(paths: cl.Paths, monkeypatch):
    """Cleanup is best-effort: if the close itself raises (cmux mid-crash), the
    error must be logged, not propagated — the caller already handled the spawn
    failure. Mutation probe: remove the try/except and this raises."""
    def boom(*a, **k):
        raise RuntimeError("cmux gone")
    monkeypatch.setattr(cs, "close_own_workspace", boom)
    logs: list[str] = []
    cs._abandon_failed_spawn(paths, "workspace:9", log=logs.append)
    assert any("cleanup after failed spawn errored" in m for m in logs)


def test_reconcile_is_instance_scoped_never_touches_other_instance(tmp_path, monkeypatch):
    """The bug: reconcile closed EVERY warm-titled workspace machine-wide, so a
    second comms instance (distinct COMMS_HOME) or a live-validation spawn closed
    the production instance's warm session. Fix: reconcile only closes refs in
    THIS instance's own spawned-workspaces ledger.

    The title-scan pass also closes untracked orphans, but it still cannot reach
    B's workspace when the title scan returns only instance-A-visible refs — and
    close_own_workspace is title-guarded as defence-in-depth."""
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
    # Title scan returns only the surviving warm workspace — no untracked orphans.
    monkeypatch.setattr(cs, "list_warm_workspaces", lambda p: ["workspace:3"])

    # Instance A reconciles after respawning ws:3 — it must close only its OWN
    # orphan (ws:1) and NEVER B's production ws:2.
    cs.record_spawned_ref(paths_a, "workspace:3")
    cs.reconcile_warm_workspaces(paths_a, keep="workspace:3")

    assert "workspace:2" not in closed, "reconcile must not touch another instance's session"
    assert closed == ["workspace:1"]
    # B's ledger is untouched; A's ledger now holds only the survivor.
    assert cs.read_spawned_refs(paths_b) == ["workspace:2"]
    assert cs.read_spawned_refs(paths_a) == ["workspace:3"]
