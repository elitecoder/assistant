"""Tests for the propose_memory tool in bin/mcp-agent-tools.py.

A proposal needs a rule and a target; the trigger line is optional.
"""
from __future__ import annotations

import importlib.util
import json
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


mat = _load("mcp_agent_tools_mod", "mcp-agent-tools.py")


def _fake_propose(monkeypatch, stdout: str = '{"status": "recorded", "proposal_id": "p1"}'):
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stderr = ""

    _Done.stdout = stdout
    monkeypatch.setattr(mat.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd) or _Done())
    return calls


def test_propose_memory_schema_requires_a_rule_and_no_trigger():
    schema = next(t for t in mat.TOOLS_LIST if t["name"] == "propose_memory")["inputSchema"]
    assert schema["required"] == ["rule", "target"]
    assert {"trigger", "rule", "target", "scope"} <= set(schema["properties"])


def test_propose_memory_without_a_rule_is_an_error(monkeypatch):
    calls = _fake_propose(monkeypatch)
    assert mat.handle_propose_memory({"trigger": "On X", "target": "claude"}) == {
        "error": "rule is required"}
    assert calls == []


def test_propose_memory_passes_a_rule_with_no_trigger(monkeypatch):
    calls = _fake_propose(monkeypatch)
    result = mat.handle_propose_memory({"rule": "When X, do Y.", "target": "claude"})
    assert result == {"status": "recorded", "proposal_id": "p1"}
    [cmd] = calls
    assert cmd[1:] == [str(mat.PROPOSE), "--trigger", "", "--rule", "When X, do Y.",
                       "--target", "claude", "--scope", "general", "--source", "agent"]


def test_propose_memory_passes_a_trigger_when_given(monkeypatch):
    calls = _fake_propose(monkeypatch)
    mat.handle_propose_memory({"trigger": "On X", "rule": "Do Y.", "target": "assistant"})
    [cmd] = calls
    assert cmd[cmd.index("--trigger") + 1] == "On X"
    assert cmd[cmd.index("--rule") + 1] == "Do Y."


def test_propose_memory_tool_call_reports_the_proposal(monkeypatch, capsys):
    _fake_propose(monkeypatch)
    mat.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                        "params": {"name": "propose_memory",
                                   "arguments": {"rule": "When X, do Y.", "target": "claude"}}})
    reply = json.loads(capsys.readouterr().out)
    assert reply["id"] == 7
    assert json.loads(reply["result"]["content"][0]["text"]) == {
        "status": "recorded", "proposal_id": "p1"}
