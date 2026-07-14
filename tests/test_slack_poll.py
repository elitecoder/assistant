"""Tests for slack-poll.py — inbound Slack history polling with cursor tracking.

Zero real egress: every test injects a fake `http`.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import comms_lib as cl
import pytest


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(Path(__file__).resolve().parent.parent / "bin" / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


slack_poll = _load("slack_poll", "slack-poll.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    p = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})
    p.config.write_text(json.dumps({"slack": {"target": "U123", "allowed_targets": ["U123"]}}))
    return p


def _run(argv, http, paths):
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = slack_poll.main(argv, http=http, paths=paths, env={"SLACK_BOT_TOKEN": "t"})
    return rc, buf.getvalue().strip(), err.getvalue().strip()


def _history_http(messages):
    """Fake http: resolves U→D, returns `messages` for conversations.history."""
    def http(token, method, params):
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D999"}}
        if method == "conversations.history":
            return {"ok": True, "messages": messages}
        raise AssertionError(method)
    return http


def test_reverses_to_oldest_first_and_filters_bot(paths: cl.Paths):
    # Slack returns newest-first; poll must reverse and drop bot/self messages.
    msgs = [
        {"ts": "1700.0003", "user": "U123", "text": "third"},
        {"ts": "1700.0002", "bot_id": "B1", "text": "my bot ping"},   # dropped
        {"ts": "1700.0001", "user": "U123", "text": "first"},
    ]
    rc, out, _err = _run([], _history_http(msgs), paths)
    assert rc == 0
    got = json.loads(out)
    assert [m["text"] for m in got] == ["first", "third"]
    assert cl.read_slack_cursor(paths) == "1700.0003"  # advanced to highest


def test_thread_reply_sets_reply_to(paths: cl.Paths):
    msgs = [{"ts": "1700.0005", "user": "U123", "text": "in a thread",
             "thread_ts": "1700.0001"}]
    rc, out, _err = _run([], _history_http(msgs), paths)
    got = json.loads(out)
    assert got[0]["reply_to"] == "1700.0001"


def test_root_message_thread_ts_equals_ts_is_not_reply(paths: cl.Paths):
    # A thread root has thread_ts == ts; that's not a "reply_to".
    msgs = [{"ts": "1700.0001", "user": "U123", "text": "root",
             "thread_ts": "1700.0001"}]
    got = json.loads(_run([], _history_http(msgs), paths)[1])
    assert got[0]["reply_to"] is None


def test_system_subtypes_skipped(paths: cl.Paths):
    msgs = [
        {"ts": "1700.2", "subtype": "channel_join", "user": "U123", "text": "joined"},
        {"ts": "1700.1", "user": "U123", "text": "real"},
    ]
    got = json.loads(_run([], _history_http(msgs), paths)[1])
    assert [m["text"] for m in got] == ["real"]


def test_cursor_passed_as_oldest(paths: cl.Paths):
    cl.write_slack_cursor(paths, "1700.0001")
    seen = {}

    def http(token, method, params):
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D999"}}
        seen["params"] = params
        return {"ok": True, "messages": []}

    _run([], http, paths)
    assert seen["params"].get("oldest") == "1700.0001"


def test_empty_history_leaves_cursor(paths: cl.Paths):
    cl.write_slack_cursor(paths, "1700.5")
    _run([], _history_http([]), paths)
    assert cl.read_slack_cursor(paths) == "1700.5"


def test_reset_cursor_advances_to_latest(paths: cl.Paths):
    msgs = [{"ts": "1700.9", "user": "U123", "text": "latest"}]
    rc, out, _err = _run(["--reset-cursor"], _history_http(msgs), paths)
    assert rc == 0 and json.loads(out) == []
    assert cl.read_slack_cursor(paths) == "1700.9"


def test_missing_target_errors(tmp_path: Path):
    home = tmp_path / "h"
    (home / ".assistant").mkdir(parents=True)
    p = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})
    p.config.write_text(json.dumps({"slack": {"allowed_targets": []}}))
    rc, _out, err = _run([], _history_http([]), p)
    assert rc == 1 and "target" in err
