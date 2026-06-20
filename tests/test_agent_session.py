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


# ── spawn policy ──────────────────────────────────────────────────────────────

def test_launch_command():
    assert ag.launch_command(ag.CLAUDE) == "claude"
    assert ag.launch_command(ag.DROID) == "droid"


def test_ready_re_matches_observed_banners():
    assert ag.ready_re(ag.CLAUDE).search("Welcome to Claude Code v2.1.177")
    assert ag.ready_re(ag.CLAUDE).search("⏵⏵ bypass permissions on")
    assert ag.ready_re(ag.DROID).search("? for help")
    assert ag.ready_re(ag.DROID).search("Auto (High) · allow all commands        Opus 4.8 (High)")
    assert ag.ready_re(ag.DROID).search("Skills (63) ✓  MCPs (1) ✓  AGENTS.md ✓")
    # A claude screen must NOT satisfy the droid gate and vice-versa.
    assert not ag.ready_re(ag.DROID).search("Welcome to Claude Code v2.1.177")


def test_dispatch_agent_policy_defaults_claude():
    assert ag.dispatch_agent({}) == "claude"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": "droid"}) == "droid"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": "DROID"}) == "droid"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": "gpt"}) == "claude"
    assert ag.dispatch_agent({"ASSISTANT_DISPATCH_AGENT": ""}) == "claude"


def test_trust_marker():
    assert ag.trust_marker(ag.CLAUDE) == "1. Yes, I trust this folder"
    assert ag.trust_marker(ag.DROID) is None
