"""Tests for comms_session.py — the PURE logic (registry, transcript, should_clear).
The cmux I/O functions are marked `pragma: no cover` and validated live, not here."""
from __future__ import annotations

import json
import os
import re
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


# ─── await_ready: readiness gate + trust-prompt auto-answer ──────────────────
#
# Regression guard for the 2026-08-21 comms outage: the old spawn code answered
# the first-launch trust prompt exactly once, at a fixed sleep(2) BEFORE the
# readiness loop. A cold Claude boot rendered that prompt after the 2s window,
# so the answer was missed and the session stalled until timeout. await_ready
# folds the answer INTO the poll, so it fires whenever the prompt appears.

TRUST = "1. Yes, I trust this folder"
READY = "⏵⏵ bypass permissions on"


class _Recorder:
    """Injectable I/O double: serves a scripted list of boot screens (the last
    frame repeats once exhausted) and records trust answers + sleeps."""

    def __init__(self, screens):
        self._screens = list(screens)
        self._i = 0
        self.answers = 0
        self.sleeps = 0
        self.reads = 0

    def read(self) -> str:
        self.reads += 1
        frame = self._screens[min(self._i, len(self._screens) - 1)]
        self._i += 1
        return frame

    def answer(self) -> None:
        self.answers += 1

    def sleep(self, _seconds) -> None:
        self.sleeps += 1


def _run(screens, *, trust_marker=TRUST, attempts=10):
    rec = _Recorder(screens)
    ready, answered = cs.await_ready(
        read_screen=rec.read,
        ready_re=re.compile(re.escape(READY)),
        trust_marker=trust_marker,
        answer_trust=rec.answer,
        attempts=attempts,
        sleep=rec.sleep,
    )
    return ready, answered, rec


def test_await_ready_ready_on_first_poll_no_trust_no_sleep():
    ready, answered, rec = _run([f"header {READY} footer"])
    assert (ready, answered) == (True, False)
    assert rec.answers == 0
    assert rec.reads == 1
    assert rec.sleeps == 0  # short-circuits before sleeping


def test_await_ready_trust_shown_late_then_ready():
    # The outage shape: two blank frames (still booting), THEN the trust prompt
    # appears (well past any fixed 2s window), then the ready banner.
    ready, answered, rec = _run([
        "booting…", "booting…",
        f"...{TRUST}...", f"...{TRUST}...",
        f"{READY}",
    ])
    assert (ready, answered) == (True, True)
    assert rec.answers == 1  # answered when the prompt finally appeared


def test_await_ready_answers_trust_exactly_once_while_prompt_persists():
    # Claude re-renders the same prompt every frame until answered; we must not
    # spray "1"+Enter repeatedly (leaks keystrokes into the REPL post-accept).
    ready, answered, rec = _run([TRUST] * 5 + [READY])
    assert (ready, answered) == (True, True)
    assert rec.answers == 1


def test_await_ready_never_ready_reports_trust_seen():
    ready, answered, rec = _run([TRUST] * 4, attempts=4)
    assert ready is False
    assert answered is True          # surfaced in the "never ready" log detail
    assert rec.reads == 4            # respects the attempts budget exactly
    assert rec.sleeps == 4


def test_await_ready_never_ready_without_trust():
    ready, answered, rec = _run(["booting…"] * 3, attempts=3)
    assert (ready, answered) == (False, False)
    assert rec.answers == 0


def test_await_ready_none_trust_marker_never_answers():
    # Droid path: trust_marker is None, so even a screen literally containing the
    # Claude trust line must never trigger an answer.
    ready, answered, rec = _run([TRUST, TRUST, READY], trust_marker=None)
    assert (ready, answered) == (True, False)
    assert rec.answers == 0


def test_await_ready_ready_wins_when_both_markers_present():
    # If a single frame shows both, readiness short-circuits and we never answer.
    ready, answered, rec = _run([f"{TRUST} {READY}"])
    assert (ready, answered) == (True, False)
    assert rec.answers == 0


def test_await_ready_uses_real_claude_markers():
    rec = _Recorder([f"...{ag.trust_marker('claude')}...", "⏵⏵ bypass permissions on (shift+tab)"])
    ready, answered = cs.await_ready(
        read_screen=rec.read,
        ready_re=ag.ready_re("claude"),
        trust_marker=ag.trust_marker("claude"),
        answer_trust=rec.answer,
        attempts=5,
        sleep=rec.sleep,
    )
    assert (ready, answered) == (True, True)
    assert rec.answers == 1


def test_await_ready_droid_marker_is_none_by_contract():
    # The whole no-misfire guarantee rests on droid having no auto-answer gate.
    assert ag.trust_marker("droid") is None


def test_ready_attempts_default_is_generous():
    # A cold Claude-on-Bedrock boot must have headroom past the old 30s window.
    assert cs.READY_ATTEMPTS >= 60


# A faithful excerpt of the REAL Claude cold-boot trust frame captured live on
# 2026-08-21 (cmux surface.read_text of the stalled warm workspace). The whole
# fix rests on the ready marker NOT appearing on this screen — otherwise
# await_ready would report ready on the trust frame and feed the boot prompt
# into an unanswered modal. Note "Claude Code'll" must NOT match "Claude Code v".
REAL_TRUST_FRAME = """\
 Accessing workspace:

 /Users/mukuls/dev/assistant

 Quick safety check: Is this a project you created or one you trust?

 Claude Code'll be able to read, edit, and execute files here.

 ⚠ This folder pre-approves 61 tool permissions in .claude/settings.local.json:
   mcp__scout__search, mcp__scout__semantic_doc_search, and 53 more
 These will apply without asking. Only proceed if you trust this configuration.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel"""


def test_real_trust_frame_does_not_false_positive_ready():
    # The load-bearing assumption of the whole fix, pinned against the real frame.
    assert ag.ready_re("claude").search(REAL_TRUST_FRAME) is None
    # …and the auto-answer trigger IS present on that frame.
    assert ag.trust_marker("claude") in REAL_TRUST_FRAME


def test_await_ready_on_real_trust_frame_then_real_ready_bar():
    # End-to-end over the real frames: trust modal (answered) → status bar (ready).
    rec = _Recorder([REAL_TRUST_FRAME, "context 5% · ⏵⏵ bypass permissions on (shift+tab)"])
    ready, answered = cs.await_ready(
        read_screen=rec.read,
        ready_re=ag.ready_re("claude"),
        trust_marker=ag.trust_marker("claude"),
        answer_trust=rec.answer,
        attempts=5,
        sleep=rec.sleep,
    )
    assert (ready, answered) == (True, True)
    assert rec.answers == 1


# ─── _positive_int_env: env-tunable parse must never crash the daemon ─────────

def test_positive_int_env_valid_override(monkeypatch):
    monkeypatch.setenv("COMMS_READY_ATTEMPTS", "45")
    assert cs._positive_int_env("COMMS_READY_ATTEMPTS", 90) == 45


@pytest.mark.parametrize("bad", ["", "90s", "oops", "3.5"])
def test_positive_int_env_malformed_falls_back(monkeypatch, bad):
    monkeypatch.setenv("COMMS_READY_ATTEMPTS", bad)
    assert cs._positive_int_env("COMMS_READY_ATTEMPTS", 90) == 90


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_positive_int_env_non_positive_falls_back(monkeypatch, bad):
    monkeypatch.setenv("COMMS_READY_ATTEMPTS", bad)
    assert cs._positive_int_env("COMMS_READY_ATTEMPTS", 90) == 90


def test_positive_int_env_missing_uses_default(monkeypatch):
    monkeypatch.delenv("COMMS_READY_ATTEMPTS", raising=False)
    assert cs._positive_int_env("COMMS_READY_ATTEMPTS", 90) == 90


# ─── spawn_session wiring: the exact call site of the 2026-08-21 outage ───────
#
# spawn_session is `pragma: no cover` (live cmux I/O), but the WIRING to
# await_ready is the literal location the outage lived. This test fakes the cmux
# boundary and stubs await_ready so a regression that hardcoded attempts, swapped
# the markers, or broke the _answer_trust closure would fail here — not silently
# pass every helper test.

def test_spawn_session_wires_await_ready_and_answers_trust(paths, monkeypatch, tmp_path):
    def fake_run(argv, timeout=None, **kw):
        if "ping" in argv:
            return (0, "", "")
        if "new-workspace" in argv:
            return (0, "created workspace:246", "")
        if "list-pane-surfaces" in argv:
            return (0, "surface:308", "")
        return (0, "", "")

    monkeypatch.setattr(cs.comms_lib, "run_cmd", fake_run)
    monkeypatch.setattr(cs, "record_spawned_ref", lambda *a, **k: None)
    monkeypatch.setattr(cs, "project_dir_for_cwd", lambda cwd, agent: tmp_path / "proj")
    monkeypatch.setattr(cs.time, "sleep", lambda *a, **k: None)  # skip the 0.5s in _answer_trust

    sends: list[tuple] = []
    monkeypatch.setattr(cs, "_cmux_rpc",
                        lambda paths, method, params, timeout=15: sends.append((method, params)))
    monkeypatch.setattr(cs, "_surface_read_text", lambda paths, ref: "SCREEN-TEXT")

    captured: dict = {}

    def spy_await(read_screen, ready_re, trust_marker, answer_trust, attempts, sleep=None):
        captured.update(ready_re=ready_re, trust_marker=trust_marker, attempts=attempts)
        # Prove the injected callables are wired to the right surface.
        assert read_screen() == "SCREEN-TEXT"
        answer_trust()
        return (False, True)  # never ready → spawn returns None, short-circuiting downstream I/O

    monkeypatch.setattr(cs, "await_ready", spy_await)

    logs: list[str] = []
    result = cs.spawn_session(paths, tmp_path / "boot.md", log=logs.append, agent="claude")

    assert result is None
    # Correct budget + per-agent markers forwarded (not hardcoded / swapped).
    assert captured["attempts"] == cs.READY_ATTEMPTS
    assert captured["trust_marker"] == ag.trust_marker("claude")
    assert captured["ready_re"].pattern == ag.ready_re("claude").pattern
    # _answer_trust sends "1" then Enter to the resolved surface, in that order.
    assert sends == [
        ("surface.send_text", {"surface_id": "surface:308", "text": "1"}),
        ("surface.send_key", {"surface_id": "surface:308", "key": "enter"}),
    ]
    # The never-ready diagnostic records that the trust prompt was seen.
    assert any("never ready" in m and "trust prompt seen; answer sent" in m for m in logs)
