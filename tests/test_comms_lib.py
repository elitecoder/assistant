"""Tests for comms_lib.py — the shared Slack comms helpers."""
from __future__ import annotations

import json
from pathlib import Path

import comms_lib as cl
import pytest


@pytest.fixture
def paths(tmp_path: Path, monkeypatch) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    return cl.Paths.from_env({
        "HOME": str(home),
        "COMMS_HOME": str(home),
        "COMMS_BIN_DIR": str(tmp_path / "bin"),
    })


def _write_config(paths: cl.Paths, **slack) -> None:
    paths.assistant_dir.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps({"slack": slack, "stale_heartbeat_sec": 1200}))


# ─── paths ────────────────────────────────────────────────────────────────

def test_paths_default_config_is_assistant_dir(paths: cl.Paths):
    assert paths.config == paths.assistant_dir / "config.json"
    assert paths.conversation == paths.comms_dir / "conversation.jsonl"
    assert paths.slack_cursor == paths.comms_dir / "slack.cursor"


def test_config_env_override_config_path(tmp_path: Path):
    custom = tmp_path / "elsewhere.json"
    p = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_CONFIG": str(custom)})
    assert p.config == custom


# ─── config + send-gate ─────────────────────────────────────────────────────

def test_config_load_reads_target_and_allowlist(paths: cl.Paths):
    _write_config(paths, target="U123", allowed_targets=["U123"])
    cfg = cl.Config.load(paths.config, env={})
    assert cfg.target == "U123"
    assert cfg.allowed_targets == ("U123",)
    assert cfg.is_allowed("U123")
    assert not cfg.is_allowed("U999")


def test_config_ping_target_env_overrides_file(paths: cl.Paths):
    _write_config(paths, target="U123", allowed_targets=["U123"])
    cfg = cl.Config.load(paths.config, env={"SLACK_PING_TARGET": "Cabc"})
    assert cfg.target == "Cabc"


def test_config_missing_file_raises(paths: cl.Paths):
    with pytest.raises(SystemExit):
        cl.Config.load(paths.config, env={})


def test_bot_token_from_env():
    assert cl.bot_token({"SLACK_BOT_TOKEN": "xoxb-x"}) == "xoxb-x"
    assert cl.bot_token({}) == ""


# ─── formatting ─────────────────────────────────────────────────────────────

def test_fmt_action_line_flags_screen_read():
    entry = {"kind": "cleanup", "key": "assistant:close:ws:5", "ws_ref": "ws:5",
             "outcome": "verified", "verified_via": "screen_read", "pulse_idx": 3,
             "evidence": "closed & <clean>"}
    line = cl.fmt_action_line(entry)
    assert "*[cleanup]* ok" in line
    assert "(!)screen_read" in line
    # mrkdwn escaping of the evidence's angle brackets + ampersand
    assert "&lt;clean&gt;" in line and "&amp;" in line


def test_fmt_action_line_maps_outcomes():
    assert "fail" in cl.fmt_action_line({"outcome": "failed"})
    assert "rej" in cl.fmt_action_line({"outcome": "rejected"})


def test_fmt_heartbeat_alert():
    body = cl.fmt_heartbeat_alert({"ws_ref": "ws:1", "status": "frozen",
                                   "last_pulse_iso": "2026-07-05T00:00:00Z"}, 720)
    assert "heartbeat stale" in body and "status=frozen" in body and "12m ago" in body


def test_fmt_workspace_signal_handles_both_key_names():
    # cmux-watcher writes "signal"/"signal_type" — accept either.
    body = cl.fmt_workspace_signal({"ws_ref": "ws:2", "signal": "needs_input",
                                    "screen_snippet": "waiting for input"})
    assert "needs your input" in body and "waiting for input" in body


def test_strip_html():
    assert cl.strip_html("<b>hi</b> &amp; bye") == "hi & bye"


def test_fmt_age():
    assert cl.fmt_age(-5) == "0s"
    assert cl.fmt_age(45) == "45s"
    assert cl.fmt_age(120) == "2m"
    assert cl.fmt_age(3720) == "1h2m"
    assert cl.fmt_age(90000) == "1d"


def test_parse_duration():
    assert cl.parse_duration("30m") == 1800
    assert cl.parse_duration("2h") == 7200
    assert cl.parse_duration("10s") == 10
    assert cl.parse_duration("nope") is None
    assert cl.parse_duration("-5m") is None


# ─── ledger cursor ──────────────────────────────────────────────────────────

def test_ledger_cursor_initialize_skips_backlog(paths: cl.Paths):
    paths.ledger.write_text("line1\nline2\n")
    cl.initialize_cursor_if_missing(paths)
    assert cl.read_ledger_cursor(paths) == paths.ledger.stat().st_size
    assert cl.read_new_ledger_lines(paths) == []


def test_read_new_ledger_lines_reads_appends(paths: cl.Paths):
    paths.ledger.write_text("")
    cl.initialize_cursor_if_missing(paths)
    with open(paths.ledger, "a") as f:
        f.write(json.dumps({"key": "a", "kind": "cleanup"}) + "\n")
        f.write("garbage-not-json\n")
        f.write(json.dumps({"key": "b"}) + "\n")
    entries = cl.read_new_ledger_lines(paths)
    assert [e["key"] for e in entries] == ["a", "b"]
    assert cl.read_new_ledger_lines(paths) == []  # cursor advanced


def test_read_new_ledger_lines_handles_rotation(paths: cl.Paths):
    # Rotation is detected only when the file shrinks below the cursor (the
    # byte-cursor tail's inherent limit — matches the original comms_lib). Start
    # with a large file, then truncate to a smaller one.
    paths.ledger.write_text((json.dumps({"key": "old-and-long-entry-number-one"}) + "\n") * 5)
    cl.initialize_cursor_if_missing(paths)
    paths.ledger.write_text(json.dumps({"key": "fresh"}) + "\n")  # shrank below cursor
    entries = cl.read_new_ledger_lines(paths)
    assert [e["key"] for e in entries] == ["fresh"]


# ─── slack cursor ───────────────────────────────────────────────────────────

def test_slack_cursor_roundtrip(paths: cl.Paths):
    assert cl.read_slack_cursor(paths) == "0"
    cl.write_slack_cursor(paths, "1700000000.000200")
    assert cl.read_slack_cursor(paths) == "1700000000.000200"


# ─── threads.jsonl ──────────────────────────────────────────────────────────

def test_threads_append_and_lookup(paths: cl.Paths):
    cl.append_thread(paths, "assistant:close:ws:5", "1700.0001", "D42", "action",
                     clock=lambda: 1700)
    cl.append_thread(paths, "assistant:close:ws:5", "1700.0002", "D43", "action",
                     clock=lambda: 1701)
    by_ts = cl.lookup_thread_by_msg_ts(paths, "1700.0001")
    assert by_ts and by_ts["channel"] == "D42"
    by_key = cl.lookup_thread_by_ledger_key(paths, "assistant:close:ws:5")
    assert len(by_key) == 2
    assert cl.lookup_thread_by_msg_ts(paths, "nope") is None


# ─── conversation.jsonl ─────────────────────────────────────────────────────

def test_conversation_append_and_window(paths: cl.Paths):
    cl.append_conversation_turn(paths, "D42", "1.1", "in", "hi", clock=lambda: 1000)
    cl.append_conversation_turn(paths, "D42", "1.2", "out", "hey", kind="reply",
                                reply_to="1.1", clock=lambda: 1001)
    cl.append_conversation_turn(paths, "D99", "9.1", "in", "other channel", clock=lambda: 1002)
    rows = cl.read_conversation_window(paths, "D42", now=lambda: 1002)
    assert [r["text"] for r in rows] == ["hi", "hey"]
    assert rows[1]["reply_to"] == "1.1"


def test_conversation_window_age_bound(paths: cl.Paths):
    cl.append_conversation_turn(paths, "D42", "1.1", "in", "old", clock=lambda: 0)
    cl.append_conversation_turn(paths, "D42", "1.2", "in", "new", clock=lambda: 10000)
    rows = cl.read_conversation_window(paths, "D42", max_age_sec=100, now=lambda: 10000)
    assert [r["text"] for r in rows] == ["new"]


def test_conversation_window_turn_bound(paths: cl.Paths):
    for i in range(30):
        cl.append_conversation_turn(paths, "D42", f"1.{i}", "in", f"m{i}", clock=lambda: 1000)
    rows = cl.read_conversation_window(paths, "D42", max_turns=5, now=lambda: 1000)
    assert [r["text"] for r in rows] == [f"m{i}" for i in range(25, 30)]


def test_conversation_bad_direction_raises(paths: cl.Paths):
    with pytest.raises(ValueError):
        cl.append_conversation_turn(paths, "D42", "1.1", "sideways", "x")


# ─── context measurement ────────────────────────────────────────────────────

def test_read_context_tokens_sums_last_usage(tmp_path: Path):
    t = tmp_path / "transcript.jsonl"
    t.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5}}}) + "\n"
        + json.dumps({"message": {"usage": {"input_tokens": 100,
                                            "cache_creation_input_tokens": 50,
                                            "cache_read_input_tokens": 350}}}) + "\n")
    assert cl.read_context_tokens(t) == 500


def test_context_fraction():
    assert cl.context_fraction(500_000) == 0.5
    assert cl.context_fraction(None) == 0.0


def test_should_clear_threshold_via_fraction(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"usage": {"input_tokens": 600_000}}}) + "\n")
    assert cl.context_fraction(cl.read_context_tokens(t)) >= 0.5


# ─── send_notification ──────────────────────────────────────────────────────

def test_send_notification_calls_slack_send_with_target(paths: cl.Paths):
    _write_config(paths, target="U123", allowed_targets=["U123"])
    captured = {}

    def runner(argv):
        captured["argv"] = argv
        class R:
            returncode = 0
        return R()

    ok = cl.send_notification("hello", paths.config, Path("/repo/bin"),
                              kind="action", runner=runner)
    assert ok
    argv = captured["argv"]
    assert "slack-send.py" in argv[1]
    assert "--channel" in argv and "U123" in argv
    assert "--kind" in argv and "action" in argv


def test_send_notification_no_target_returns_false(paths: cl.Paths):
    _write_config(paths, allowed_targets=[])  # no target
    assert cl.send_notification("x", paths.config, Path("/repo/bin")) is False
