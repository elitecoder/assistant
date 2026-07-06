"""Tests for thread-reply polling — the gap where conversations.history omits
in-thread replies, so the daemon must poll conversations.replies per open thread.

Covers: comms_lib open-threads helpers, slack-send thread registration, and
slack-poll's replies pass. Zero real egress (injected http)."""
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


slack_send = _load("slack_send", "slack-send.py")
slack_poll = _load("slack_poll", "slack-poll.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    p = cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})
    p.config.write_text(json.dumps({"slack": {"target": "C0", "allowed_targets": ["C0"]}}))
    return p


# ─── comms_lib open-threads helpers ─────────────────────────────────────────

def test_register_and_read_open_thread(paths: cl.Paths):
    cl.register_open_thread(paths, "100.1", "C0", seen_ts="100.1", clock=lambda: 1000)
    ot = cl.read_open_threads(paths)
    assert ot["100.1"]["channel"] == "C0"
    assert ot["100.1"]["cursor"] == "100.1"
    assert ot["100.1"]["last_seen"] == 1000


def test_register_advances_cursor_but_never_backwards(paths: cl.Paths):
    cl.register_open_thread(paths, "100.1", "C0", seen_ts="100.5", clock=lambda: 1)
    cl.register_open_thread(paths, "100.1", "C0", seen_ts="100.3", clock=lambda: 2)  # older
    assert cl.read_open_threads(paths)["100.1"]["cursor"] == "100.5"


def test_set_thread_cursor(paths: cl.Paths):
    cl.register_open_thread(paths, "100.1", "C0", seen_ts="100.1", clock=lambda: 1)
    cl.set_thread_cursor(paths, "100.1", "100.9", clock=lambda: 2)
    assert cl.read_open_threads(paths)["100.1"]["cursor"] == "100.9"
    # unknown thread is a no-op
    cl.set_thread_cursor(paths, "999.9", "999.9", clock=lambda: 3)
    assert "999.9" not in cl.read_open_threads(paths)


def test_prune_by_ttl(paths: cl.Paths, monkeypatch):
    monkeypatch.setattr(cl, "OPEN_THREAD_TTL_SEC", 100)
    cl.register_open_thread(paths, "old.1", "C0", seen_ts="old.1", clock=lambda: 0)
    # a later registration with now=1000 prunes the >100s-idle "old.1"
    cl.register_open_thread(paths, "new.1", "C0", seen_ts="new.1", clock=lambda: 1000)
    ot = cl.read_open_threads(paths)
    assert "old.1" not in ot and "new.1" in ot


def test_prune_caps_to_max(paths: cl.Paths, monkeypatch):
    monkeypatch.setattr(cl, "MAX_OPEN_THREADS", 3)
    for i in range(6):
        cl.register_open_thread(paths, f"1.{i}", "C0", seen_ts=f"1.{i}", clock=lambda i=i: 1000 + i)
    ot = cl.read_open_threads(paths)
    assert len(ot) == 3
    # keeps the most recently active (highest last_seen)
    assert set(ot) == {"1.3", "1.4", "1.5"}


# ─── slack-send registers the thread it posts into ──────────────────────────

def _send(paths, argv, http):
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = slack_send.main(argv, http=http, paths=paths, env={"SLACK_BOT_TOKEN": "t"})
    return rc, buf.getvalue().strip()


def test_top_level_send_registers_root_at_own_ts(paths: cl.Paths):
    def http(t, m, p):
        return {"ok": True, "channel": "C0", "ts": "200.1"}
    rc, _ = _send(paths, ["--channel", "C0", "--text", "hi", "--kind", "action"], http)
    assert rc == 0
    ot = cl.read_open_threads(paths)
    assert "200.1" in ot and ot["200.1"]["cursor"] == "200.1"


def test_reply_send_registers_root_at_reply_to(paths: cl.Paths):
    def http(t, m, p):
        return {"ok": True, "channel": "C0", "ts": "200.9"}
    rc, _ = _send(paths, ["--channel", "C0", "--text", "re", "--kind", "reply",
                          "--reply-to", "200.1"], http)
    assert rc == 0
    ot = cl.read_open_threads(paths)
    # the thread root is the reply-to, not the new message ts
    assert "200.1" in ot and "200.9" not in ot
    assert ot["200.1"]["cursor"] == "200.9"  # seeded to our just-sent ts


# ─── slack-poll fetches thread replies ──────────────────────────────────────

def _poll(paths, http, argv=None):
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = slack_poll.main(argv or ["--bot-user-id", "U0BOT"], http=http, paths=paths,
                             env={"SLACK_BOT_TOKEN": "t"})
    return rc, json.loads(buf.getvalue().strip())


def _http(history=None, replies_by_thread=None):
    history = history or []
    replies_by_thread = replies_by_thread or {}

    def http(token, method, params):
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "C0"}}
        if method == "conversations.history":
            return {"ok": True, "messages": history}
        if method == "conversations.replies":
            oldest = float(params.get("oldest", "0") or 0)
            msgs = replies_by_thread.get(params["ts"], [])
            return {"ok": True, "messages": [m for m in msgs if float(m["ts"]) > oldest
                                             or m["ts"] == params["ts"]]}
        raise AssertionError(method)
    return http


def test_thread_reply_is_fetched(paths: cl.Paths):
    cl.register_open_thread(paths, "300.1", "C0", seen_ts="300.1", clock=lambda: 1)
    http = _http(replies_by_thread={"300.1": [
        {"ts": "300.1", "user": "U1", "text": "root", "thread_ts": "300.1"},
        {"ts": "300.2", "user": "UM", "text": "in-thread reply", "thread_ts": "300.1"},
    ]})
    rc, out = _poll(paths, http)
    assert rc == 0 and len(out) == 1
    assert out[0]["text"] == "in-thread reply"
    assert out[0]["reply_to"] == "300.1" and out[0]["msg_ts"] == "300.2"


def test_thread_root_never_redelivered(paths: cl.Paths):
    cl.register_open_thread(paths, "300.1", "C0", seen_ts="300.1", clock=lambda: 1)
    http = _http(replies_by_thread={"300.1": [
        {"ts": "300.1", "user": "U1", "text": "root", "thread_ts": "300.1"},
    ]})
    rc, out = _poll(paths, http)
    assert out == []  # only the root exists → nothing new


def test_thread_cursor_advances_no_redelivery(paths: cl.Paths):
    cl.register_open_thread(paths, "300.1", "C0", seen_ts="300.1", clock=lambda: 1)
    http = _http(replies_by_thread={"300.1": [
        {"ts": "300.1", "user": "U1", "text": "root", "thread_ts": "300.1"},
        {"ts": "300.2", "user": "UM", "text": "first", "thread_ts": "300.1"},
    ]})
    _, out1 = _poll(paths, http)
    _, out2 = _poll(paths, http)
    assert len(out1) == 1 and out2 == []
    assert cl.read_open_threads(paths)["300.1"]["cursor"] == "300.2"


def test_channel_thread_root_gets_registered(paths: cl.Paths):
    # A top-level inbound message becomes a watched thread so future replies
    # under it are polled — even across a restart with no prior registration.
    http = _http(history=[{"ts": "400.1", "user": "UM", "text": "new top-level"}])
    rc, out = _poll(paths, http)
    assert rc == 0 and len(out) == 1 and out[0]["msg_ts"] == "400.1"
    assert "400.1" in cl.read_open_threads(paths)


def test_deleted_thread_does_not_wedge_poll(paths: cl.Paths):
    cl.register_open_thread(paths, "500.1", "C0", seen_ts="500.1", clock=lambda: 1)

    def http(token, method, params):
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "C0"}}
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "600.1", "user": "UM", "text": "still works"}]}
        if method == "conversations.replies":
            raise RuntimeError("slack error: thread_not_found")
        raise AssertionError(method)

    rc, out = _poll(paths, http)
    # the channel message still comes through despite the thread error
    assert rc == 0 and any(m["text"] == "still works" for m in out)


def test_merged_output_sorted_oldest_first(paths: cl.Paths):
    cl.register_open_thread(paths, "700.1", "C0", seen_ts="700.1", clock=lambda: 1)
    http = _http(
        history=[{"ts": "800.5", "user": "UM", "text": "channel-late"}],
        replies_by_thread={"700.1": [
            {"ts": "700.1", "user": "U1", "text": "root", "thread_ts": "700.1"},
            {"ts": "700.2", "user": "UM", "text": "thread-early", "thread_ts": "700.1"},
        ]})
    rc, out = _poll(paths, http)
    texts = [m["text"] for m in out]
    assert texts == ["thread-early", "channel-late"], texts  # 700.2 < 800.5
