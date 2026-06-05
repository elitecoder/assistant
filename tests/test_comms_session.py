"""Tests for comms_session pure logic (registry, transcript, should_clear).

The cmux I/O (spawn/feed/clear) is validated live, not mocked — mocking the
RPC would test the mock, not the integration. These cover everything that can
be exercised without a live cmux socket."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import comms_lib as cl
import comms_session as cs


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"; home.mkdir()
    p = cl.Paths.from_env({
        "HOME": str(home), "COMMS_HOME": str(home),
        "COMMS_ASSISTANT_DIR": str(tmp_path / "assistant"),
        "COMMS_BIN_DIR": str(tmp_path / "bin"), "CMUX_BIN": "/bin/true",
    })
    p.comms_dir.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- registry

class TestSessionRegistry:
    def test_read_missing(self, paths):
        assert cs.read_session(paths) is None

    def test_write_then_read(self, paths):
        cs.write_session(paths, "workspace:7", "surface:9", "/cwd", "/t.jsonl", clock=lambda: 123)
        rec = cs.read_session(paths)
        assert rec["ws_ref"] == "workspace:7"
        assert rec["surface_ref"] == "surface:9"
        assert rec["cwd"] == "/cwd"
        assert rec["transcript_path"] == "/t.jsonl"
        assert rec["spawned_ts"] == 123

    def test_read_unparseable(self, paths):
        cs.session_registry_path(paths).write_text("not json")
        assert cs.read_session(paths) is None

    def test_clear_registry(self, paths):
        cs.write_session(paths, "workspace:1", "surface:1", "/c", None)
        assert cs.read_session(paths) is not None
        cs.clear_session_registry(paths)
        assert cs.read_session(paths) is None

    def test_clear_registry_missing_is_noop(self, paths):
        cs.clear_session_registry(paths)  # no exception
        assert cs.read_session(paths) is None

    def test_write_default_clock(self, paths):
        cs.write_session(paths, "workspace:1", "surface:1", "/c", None)
        assert cs.read_session(paths)["spawned_ts"] > 0


# --------------------------------------------------------------------------- transcript resolution

class TestTranscriptResolution:
    def test_project_dir_slug(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cs, "HOME", tmp_path)
        d = cs.project_dir_for_cwd("/Users/x/dev/assistant")
        assert d == tmp_path / ".claude/projects" / "-Users-x-dev-assistant"

    def test_newest_transcript_none_when_no_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cs, "HOME", tmp_path)
        assert cs.newest_transcript("/no/such/cwd") is None

    def test_newest_transcript_picks_most_recent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cs, "HOME", tmp_path)
        cwd = "/cwd"
        pdir = cs.project_dir_for_cwd(cwd)
        pdir.mkdir(parents=True)
        old = pdir / "old.jsonl"; old.write_text("{}\n")
        new = pdir / "new.jsonl"; new.write_text("{}\n")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        assert cs.newest_transcript(cwd) == str(new)

    def test_newest_transcript_empty_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cs, "HOME", tmp_path)
        pdir = cs.project_dir_for_cwd("/cwd"); pdir.mkdir(parents=True)
        assert cs.newest_transcript("/cwd") is None


class TestLastAssistantText:
    def test_string_content(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": "hello"}}) + "\n")
        assert cs.last_assistant_text(p) == "hello"

    def test_list_content_joins_text_blocks(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "part1 "},
            {"type": "tool_use", "name": "x"},
            {"type": "text", "text": "part2"},
        ]}}) + "\n")
        assert cs.last_assistant_text(p) == "part1 part2"

    def test_last_wins(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps({"type": "assistant", "message": {"content": "first"}}) + "\n" +
            json.dumps({"type": "assistant", "message": {"content": "second"}}) + "\n")
        assert cs.last_assistant_text(p) == "second"

    def test_ignores_user_turns(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps({"type": "assistant", "message": {"content": "A"}}) + "\n" +
            json.dumps({"type": "user", "message": {"content": "U"}}) + "\n")
        assert cs.last_assistant_text(p) == "A"

    def test_none_when_no_assistant(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(json.dumps({"type": "user", "message": {"content": "U"}}) + "\n")
        assert cs.last_assistant_text(p) is None

    def test_missing_file(self, tmp_path):
        assert cs.last_assistant_text(tmp_path / "nope.jsonl") is None

    def test_skips_malformed_blank(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("bad\n\n" + json.dumps({"type": "assistant", "message": {"content": "ok"}}) + "\n")
        assert cs.last_assistant_text(p) == "ok"

    def test_message_not_dict(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps({"type": "assistant", "message": "str"}) + "\n" +
            json.dumps({"type": "assistant", "message": {"content": "real"}}) + "\n")
        assert cs.last_assistant_text(p) == "real"

    def test_empty_list_content_leaves_prior(self, tmp_path):
        # An assistant turn that's all tool_use (no text) shouldn't blank out
        # a prior real text answer.
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps({"type": "assistant", "message": {"content": "answer"}}) + "\n" +
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}) + "\n")
        assert cs.last_assistant_text(p) == "answer"


class TestTranscriptLineCount:
    def test_missing(self, tmp_path):
        assert cs.transcript_line_count(tmp_path / "nope.jsonl") == 0

    def test_counts_nonblank(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("a\n\nb\n\n\nc\n")
        assert cs.transcript_line_count(p) == 3


class TestShouldClear:
    def _transcript(self, tmp_path, total_tokens):
        p = tmp_path / "t.jsonl"
        p.write_text(json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": total_tokens}},
        }) + "\n")
        return p

    def test_under_threshold(self, tmp_path):
        assert cs.should_clear(self._transcript(tmp_path, 400_000)) is False

    def test_at_threshold(self, tmp_path):
        assert cs.should_clear(self._transcript(tmp_path, 500_000)) is True

    def test_over_threshold(self, tmp_path):
        assert cs.should_clear(self._transcript(tmp_path, 750_000)) is True

    def test_custom_threshold(self, tmp_path):
        assert cs.should_clear(self._transcript(tmp_path, 300_000), threshold=0.25) is True

    def test_no_usage_is_false(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": "hi"}}) + "\n")
        assert cs.should_clear(p) is False
