"""Hermetic tests for the Claude/Droid coexistence seam (`bin/agent_session.py`).

Pins the two facts every reader + the dispatcher rely on: (1) role
normalization across the two transcript schemas, and (2) the spawn-policy
constants (launch command, readiness markers, dispatch-agent resolution).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, fname: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(REPO / "bin" / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ag = _load("agent_session_mod", "agent_session.py")


# ── record_role: the load-bearing normalization ──────────────────────────────

def test_record_role_claude_schema():
    assert ag.record_role({"type": "user", "message": {"role": "user", "content": "hi"}}) == "user"
    assert ag.record_role({"type": "assistant", "message": {"role": "assistant", "content": []}}) == "assistant"


def test_record_role_droid_schema():
    # Droid: top-level type is "message"; the role rides on message.role.
    assert ag.record_role({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}) == "user"
    assert ag.record_role({"type": "message", "message": {"role": "assistant", "content": []}}) == "assistant"


def test_record_role_non_turn_records_are_none():
    assert ag.record_role({"type": "session_start", "cwd": "/x", "version": "0.153.1"}) is None
    assert ag.record_role({"type": "summary"}) is None
    assert ag.record_role({"type": "message", "message": {"role": "system"}}) is None
    assert ag.record_role({"type": "message"}) is None  # no message obj
    assert ag.record_role("not a dict") is None
    assert ag.record_role({}) is None


# ── transcript roots / slug ───────────────────────────────────────────────────

def test_transcript_root_per_agent():
    assert ag.transcript_root(ag.CLAUDE).name == "projects"
    assert ag.transcript_root(ag.CLAUDE).parent.name == ".claude"
    assert ag.transcript_root(ag.DROID).name == "sessions"
    assert ag.transcript_root(ag.DROID).parent.name == ".factory"


def test_project_slug_and_confirm_dir_share_convention():
    slug = ag.project_slug("/Users/x/dev/assistant")
    assert slug == "-Users-x-dev-assistant" or slug.endswith("-dev-assistant")
    cd_claude = ag.confirm_dir(ag.CLAUDE, "/tmp")
    cd_droid = ag.confirm_dir(ag.DROID, "/tmp")
    # Same slug under each agent's root.
    assert cd_claude.name == cd_droid.name
    assert cd_claude.parent == ag.transcript_root(ag.CLAUDE)
    assert cd_droid.parent == ag.transcript_root(ag.DROID)
    assert ag.confirm_dir(ag.DROID, "/tmp", home="/sandbox").parent == (
        Path("/sandbox/.factory/sessions"))


# ── spawn policy ──────────────────────────────────────────────────────────────

def test_launch_command(tmp_path):
    assert ag.launch_command(ag.CLAUDE) == "claude"
    lessons = tmp_path / ".claude/CLAUDE.md"
    lessons.parent.mkdir(parents=True)
    lessons.write_text("rules")
    command = ag.launch_command(ag.DROID, home=tmp_path)
    assert command.startswith("droid --settings ")
    assert "droid-glm-settings.json" in command
    assert "--auto high" in command
    assert "--append-system-prompt-file" in command
    lessons.unlink()
    assert "--append-system-prompt-file" not in ag.launch_command(
        ag.DROID, home=tmp_path)


def test_ready_re_matches_observed_banners():
    assert ag.ready_re(ag.CLAUDE).search("Welcome to Claude Code v2.1.177")
    assert ag.ready_re(ag.CLAUDE).search("⏵⏵ bypass permissions on")
    assert ag.ready_re(ag.DROID).search("? for help")
    assert ag.ready_re(ag.DROID).search("Auto (High) · allow all commands        Opus 4.8 (High)")
    assert ag.ready_re(ag.DROID).search("Skills (63) ✓  MCPs (1) ✓  AGENTS.md ✓")
    # A claude screen must NOT satisfy the droid gate and vice-versa.
    assert not ag.ready_re(ag.DROID).search("Welcome to Claude Code v2.1.177")


def test_dispatch_agent_policy_defaults_claude():
    # Fail-closed to the always-present agent: no config / unknown / empty →
    # claude, so a droid-less box never spawns a dead workspace by default.
    # Droid is opt-in only.
    assert ag.dispatch_agent({}) == "claude"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": "droid"}) == "droid"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": "DROID"}) == "droid"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": "gpt"}) == "claude"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": ""}) == "claude"


def test_trust_marker():
    assert ag.trust_marker(ag.CLAUDE) == "1. Yes, I trust this folder"
    assert ag.trust_marker(ag.DROID) is None


def _write_dispatch_config(home, agent):
    import json
    d = home / ".assistant" / "comms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"dispatch": {"agent": agent}}))


def test_dispatch_agent_uses_install_time_config(tmp_path, monkeypatch):
    # Production call (env=None) honors the install-time choice in comms/config.json.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASSISTANT_DISPATCH_AGENT", raising=False)
    _write_dispatch_config(tmp_path, "droid")
    assert ag.dispatch_agent() == "droid"
    _write_dispatch_config(tmp_path, "claude")
    assert ag.dispatch_agent() == "claude"


def test_env_override_beats_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_dispatch_config(tmp_path, "droid")
    monkeypatch.setenv("ASSISTANT_DISPATCH_AGENT", "claude")
    assert ag.dispatch_agent() == "claude"  # env wins over persisted config


def test_dispatch_agent_defaults_claude_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASSISTANT_DISPATCH_AGENT", raising=False)
    assert ag.dispatch_agent() == "claude"  # no config, no env → default
    # malformed config → treated as absent, not a crash
    d = tmp_path / ".assistant" / "comms"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{not json")
    assert ag.dispatch_agent() == "claude"
    # unknown agent value → ignored
    _write_dispatch_config(tmp_path, "gpt")
    assert ag.dispatch_agent() == "claude"


def test_explicit_env_arg_is_pure_policy_ignores_config(tmp_path, monkeypatch):
    # Passing env explicitly (the unit-test shape) must NOT read config.
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_dispatch_config(tmp_path, "droid")
    assert ag.dispatch_agent({}) == "claude"


def test_agent_available_preflights_only_droid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ag.shutil, "which", lambda _b: None)
    # claude is launched via the ~/.zprofile alias (not on PATH) → assumed present.
    assert ag.agent_available(ag.CLAUDE) is True
    # droid absent from PATH and known install locations → not available (caller
    # falls back to claude).
    assert ag.agent_available(ag.DROID) is False
    # droid at Factory's default ~/.local/bin — INVISIBLE to launchd's pinned
    # PATH (so shutil.which misses it) but found by the known-location probe, so
    # a real droid install isn't a false-negative (M8 review PATH fix).
    d = tmp_path / ".local" / "bin"
    d.mkdir(parents=True)
    (d / "droid").write_text("#!/bin/sh\n")
    assert ag.agent_available(ag.DROID) is True
    # or resolvable on PATH directly.
    monkeypatch.setattr(ag.shutil, "which",
                        lambda b: "/x/droid" if b == "droid" else None)
    assert ag.agent_available(ag.DROID) is True
