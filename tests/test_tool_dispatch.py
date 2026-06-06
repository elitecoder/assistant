"""Tests for bin/tool-dispatch.py — the named-tool dispatcher.

Exercises arg validation (the dispatcher's whole job) plus an end-to-end
dispatch against a throwaway manifest + tool script, so the contract
"validate → exec → pass stdout through, JSON error + exit 1 on failure" is
covered for real rather than mocked.
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


td = _load("tool_dispatch_mod", "tool-dispatch.py")


# ─── arg parsing / validation (pure) ─────────────────────────────────────────

def test_parse_args_coerces_int_and_passes_string():
    spec = [
        {"name": "n", "type": "int", "required": False},
        {"name": "ws", "type": "string", "required": False},
    ]
    got = td.parse_tool_args(spec, ["--n", "5", "--ws", "workspace:12"])
    assert got == {"n": 5, "ws": "workspace:12"}


def test_parse_args_missing_required_raises():
    spec = [{"name": "ws", "type": "string", "required": True}]
    with pytest.raises(ValueError, match="missing required argument"):
        td.parse_tool_args(spec, [])


def test_parse_args_bad_int_raises():
    spec = [{"name": "n", "type": "int", "required": False}]
    with pytest.raises(ValueError, match="expected int"):
        td.parse_tool_args(spec, ["--n", "notanint"])


def test_parse_args_unknown_flag_raises():
    spec = [{"name": "n", "type": "int", "required": False}]
    with pytest.raises(ValueError, match="unknown argument"):
        td.parse_tool_args(spec, ["--bogus", "1"])


def test_parse_args_bad_choice_raises():
    spec = [{"name": "target", "type": "string", "required": False,
             "choices": ["assistant", "claude"]}]
    with pytest.raises(ValueError, match="must be one of"):
        td.parse_tool_args(spec, ["--target", "nonsense"])


def test_parse_args_missing_value_raises():
    spec = [{"name": "ws", "type": "string", "required": False}]
    with pytest.raises(ValueError, match="expects a value"):
        td.parse_tool_args(spec, ["--ws"])


# ─── manifest loading ────────────────────────────────────────────────────────

def test_load_manifest_missing_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="manifest not found"):
        td.load_manifest(tmp_path / "nope.json")


def test_load_manifest_bad_json_raises(tmp_path: Path):
    p = tmp_path / "m.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        td.load_manifest(p)


def test_real_manifest_loads_and_has_six_tools():
    manifest = td.load_manifest()
    names = {t["name"] for t in manifest}
    assert {"fleet_status", "workspace_peek", "recent_actions",
            "thread_context", "propose_lesson", "system_health"} <= names


# ─── end-to-end dispatch (real subprocess) ───────────────────────────────────

def _stub_manifest(tmp_path: Path, monkeypatch) -> Path:
    """Point the dispatcher at a tmp repo with one echo tool."""
    tool = tmp_path / "bin" / "tools" / "echo-tool.py"
    tool.parent.mkdir(parents=True)
    tool.write_text(
        "import argparse, json\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--msg', required=True)\n"
        "a = ap.parse_args()\n"
        "print(json.dumps({'echo': a.msg}))\n"
    )
    manifest = tmp_path / "bin" / "tools-manifest.json"
    manifest.write_text(json.dumps([
        {"name": "echo_tool",
         "description": "echo a message",
         "args": [{"name": "msg", "type": "string", "required": True}],
         "script": "bin/tools/echo-tool.py"}
    ]))
    monkeypatch.setattr(td, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(td, "REPO", tmp_path)
    return manifest


def test_dispatch_end_to_end_passes_stdout(tmp_path, monkeypatch, capsys):
    _stub_manifest(tmp_path, monkeypatch)
    rc = td.dispatch("echo_tool", ["--msg", "hello"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == {"echo": "hello"}


def test_dispatch_unknown_tool_json_error(tmp_path, monkeypatch, capsys):
    _stub_manifest(tmp_path, monkeypatch)
    rc = td.dispatch("ghost", [])
    out = capsys.readouterr().out
    assert rc == 1
    err = json.loads(out)
    assert err["tool"] == "ghost" and "unknown tool" in err["error"]


def test_dispatch_missing_required_arg_json_error(tmp_path, monkeypatch, capsys):
    _stub_manifest(tmp_path, monkeypatch)
    rc = td.dispatch("echo_tool", [])
    out = capsys.readouterr().out
    assert rc == 1
    assert "missing required argument" in json.loads(out)["error"]


def test_dispatch_missing_script_json_error(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "bin" / "tools-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps([
        {"name": "gone", "description": "x", "args": [],
         "script": "bin/tools/does-not-exist.py"}
    ]))
    monkeypatch.setattr(td, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(td, "REPO", tmp_path)
    rc = td.dispatch("gone", [])
    out = capsys.readouterr().out
    assert rc == 1
    assert "tool script missing" in json.loads(out)["error"]


def test_main_list_emits_tool_names(capsys):
    rc = td.main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    names = {t["name"] for t in json.loads(out)}
    assert "fleet_status" in names
