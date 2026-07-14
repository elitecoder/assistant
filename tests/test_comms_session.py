"""Tests for comms_session.py — the PURE logic (registry, transcript, should_clear).
The cmux I/O functions are marked `pragma: no cover` and validated live, not here."""
from __future__ import annotations

import json
import os
from pathlib import Path

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
