"""Tests for comms-listen.py inbox drain — the freshness gate specifically.

Regression: a weeks-old backlog of cmux-watcher signals must NOT ping (observed
live 2026-07-06 — 5 signals from June 9 got blasted to Slack on daemon start).
Fresh signals still ping. Zero real egress: cli() is monkeypatched.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import comms_lib as cl
import pytest


def _load():
    if "comms_listen" in sys.modules:
        return sys.modules["comms_listen"]
    spec = importlib.util.spec_from_file_location(
        "comms_listen", str(Path(__file__).resolve().parent.parent / "bin" / "comms-listen.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comms_listen"] = mod
    # comms-listen imports comms_session at module load; it's importable via bin/ on sys.path.
    spec.loader.exec_module(mod)
    return mod


listen = _load()


@pytest.fixture
def env_inbox(tmp_path: Path, monkeypatch):
    """Root the inbox + config under tmp_path and capture every send."""
    home = tmp_path / "home"
    (home / ".assistant" / "inbox").mkdir(parents=True)
    (home / ".assistant" / "config.json").write_text(json.dumps(
        {"slack": {"target": "C0", "allowed_targets": ["C0"]}}))
    monkeypatch.setenv("COMMS_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    inbox = home / ".assistant" / "inbox"
    monkeypatch.setattr(listen, "INBOX_DIR", inbox)

    sent = []

    def fake_cli(argv, timeout=30, env=None):
        if "slack-send.py" in argv[0]:
            sent.append(argv)
            return (0, json.dumps({"channel": "C0", "message_id": "1.1", "kind": "action"}), "")
        return (0, "", "")

    monkeypatch.setattr(listen, "cli", fake_cli)
    return inbox, sent


def _write_signal(inbox: Path, name: str, ts_iso: str | None, **fields):
    item = {"ws_ref": fields.get("ws_ref", "workspace:1"),
            "signal": fields.get("signal", "needs_input")}
    if ts_iso is not None:
        item["ts"] = ts_iso
    p = inbox / name
    p.write_text(json.dumps(item))
    return p


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_stale_signal_dropped_without_ping(env_inbox):
    inbox, sent = env_inbox
    old = _write_signal(inbox, "cmux-old.json", _iso(time.time() - 26 * 86400))
    n = listen._drain_inbox_once({})
    assert n == 0
    assert sent == [], "a 26-day-old signal must NOT ping"
    assert not old.exists(), "stale signal should be deleted"


def test_fresh_signal_pings(env_inbox):
    inbox, sent = env_inbox
    fresh = _write_signal(inbox, "cmux-fresh.json", _iso(time.time() - 5))
    n = listen._drain_inbox_once({})
    assert n == 1 and len(sent) == 1
    assert not fresh.exists()
    body_idx = sent[0].index("--text") + 1
    assert "needs your input" in sent[0][body_idx]


def test_mixed_backlog_only_fresh_pings(env_inbox):
    inbox, sent = env_inbox
    _write_signal(inbox, "cmux-1-old.json", _iso(time.time() - 3600))
    _write_signal(inbox, "cmux-2-fresh.json", _iso(time.time() - 2), ws_ref="workspace:9")
    _write_signal(inbox, "cmux-3-old.json", _iso(time.time() - 7200))
    n = listen._drain_inbox_once({})
    assert n == 1, "only the one fresh signal should ping"
    assert len(sent) == 1
    assert list(inbox.glob("cmux-*.json")) == []  # all consumed (fresh pinged, stale dropped)


def test_no_ts_falls_back_to_mtime(env_inbox, monkeypatch):
    inbox, sent = env_inbox
    p = _write_signal(inbox, "cmux-nots.json", None)
    # backdate the file mtime well past the window
    old = time.time() - 10000
    import os
    os.utime(p, (old, old))
    n = listen._drain_inbox_once({})
    assert n == 0 and sent == [] and not p.exists()
