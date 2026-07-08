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


# ─── proposals delivery (_drain_proposals_once) ─────────────────────────────
#
# The regression this closes: lesson proposals written to proposals.jsonl never
# reached the operator. proposals_loop now delivers each new pending lesson
# proposal to Slack exactly once. Zero real egress — cli() is monkeypatched.


@pytest.fixture
def env_proposals(tmp_path: Path, monkeypatch):
    """Root proposals.jsonl + cursor + config under tmp_path; capture sends."""
    home = tmp_path / "home"
    (home / ".assistant" / "comms").mkdir(parents=True)
    (home / ".assistant" / "config.json").write_text(json.dumps(
        {"slack": {"target": "C0", "allowed_targets": ["C0"]}}))
    monkeypatch.setenv("COMMS_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)

    paths = cl.Paths.from_env()
    sent = []
    convo_appends = []

    def fake_cli(argv, timeout=30, env=None):
        if "slack-send.py" in argv[0]:
            sent.append(argv)
            return (0, json.dumps({"channel": "C0", "message_id": f"1.{len(sent)}",
                                   "kind": "action"}), "")
        if "conversation.py" in argv[0]:
            convo_appends.append(argv)
            return (0, "", "")
        return (0, "", "")

    monkeypatch.setattr(listen, "cli", fake_cli)
    return paths, sent, convo_appends


def _lesson(pid: str, status: str = "pending", **extra) -> dict:
    return {"id": pid, "ts": pid, "type": "lesson", "status": status,
            "trigger": f"trig-{pid}", "rule": f"rule-{pid}",
            "target": "assistant", "scope": "general", **extra}


def _write_proposals(paths, *entries):
    paths.proposals.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_drain_proposals_pings_new_pending_lesson(env_proposals):
    paths, sent, convo = env_proposals
    _write_proposals(paths, _lesson("2026-07-08T10:00:00.000000Z"))
    cl.write_proposals_cursor(paths, "")  # deliver-all
    n = listen._drain_proposals_once({}, paths)
    assert n == 1 and len(sent) == 1
    body_idx = sent[0].index("--text") + 1
    assert "Lesson proposal" in sent[0][body_idx]
    assert "2026-07-08T10:00:00.000000Z" in sent[0][body_idx]
    # ledger-key carries the id for thread resolution
    key_idx = sent[0].index("--ledger-key") + 1
    assert sent[0][key_idx] == "proposal:2026-07-08T10:00:00.000000Z"
    # cursor advanced to the delivered id
    assert cl.read_proposals_cursor(paths) == "2026-07-08T10:00:00.000000Z"
    # mirrored into conversation.jsonl
    assert any("conversation.py" in a[0] for a in convo)


def test_drain_proposals_exactly_once(env_proposals):
    paths, sent, _ = env_proposals
    _write_proposals(paths, _lesson("2026-07-08T10:00:00.000000Z"))
    cl.write_proposals_cursor(paths, "")
    assert listen._drain_proposals_once({}, paths) == 1
    # Second drain over the same file: nothing new to deliver.
    assert listen._drain_proposals_once({}, paths) == 0
    assert len(sent) == 1, "a delivered proposal never re-pings"


def test_drain_proposals_skips_pattern_and_confirmed(env_proposals):
    paths, sent, _ = env_proposals
    _write_proposals(paths,
                     _lesson("2026-07-08T10:00:00.000000Z", status="confirmed"),
                     {"id": "2026-07-08T10:00:01.000000Z", "type": "pattern",
                      "status": "pending"},
                     _lesson("2026-07-08T10:00:02.000000Z"))
    cl.write_proposals_cursor(paths, "")
    n = listen._drain_proposals_once({}, paths)
    assert n == 1 and len(sent) == 1
    body_idx = sent[0].index("--text") + 1
    assert "2026-07-08T10:00:02.000000Z" in sent[0][body_idx]


def test_drain_proposals_halts_and_retries_on_send_failure(env_proposals, monkeypatch):
    # THREE proposals, the MIDDLE one fails. The drain must HALT at the failure
    # (not skip past it): if it continued to the third and advanced the cursor
    # past the failed middle one, that proposal would be lost forever. This is
    # what distinguishes `break` from `continue` — with only two proposals the
    # two behave identically, so the third is load-bearing for the mutation.
    paths, sent, _ = env_proposals
    _write_proposals(paths,
                     _lesson("2026-07-08T10:00:00.000000Z"),
                     _lesson("2026-07-08T10:00:01.000000Z"),
                     _lesson("2026-07-08T10:00:02.000000Z"))
    cl.write_proposals_cursor(paths, "")

    def failing_cli(argv, timeout=30, env=None):
        if "slack-send.py" in argv[0]:
            key = argv[argv.index("--ledger-key") + 1]
            if key.endswith("10:00:01.000000Z"):  # the middle one fails
                return (1, "", "slack 500")
            sent.append(argv)
            return (0, json.dumps({"channel": "C0", "message_id": "1.x"}), "")
        return (0, "", "")

    monkeypatch.setattr(listen, "cli", failing_cli)
    n = listen._drain_proposals_once({}, paths)
    assert n == 1, "only the first send succeeded before the halt"
    # cursor advanced only past the FIRST — the failed middle one (and the third
    # behind it) retry next pass. Never skipped past a failure.
    assert cl.read_proposals_cursor(paths) == "2026-07-08T10:00:00.000000Z"


def test_drain_proposals_backlog_skipped_on_first_run(env_proposals):
    paths, sent, _ = env_proposals
    # Simulate the real world: a big backlog already on disk, no cursor yet.
    _write_proposals(paths,
                     _lesson("2026-06-08T05:14:26.000000Z"),
                     _lesson("2026-06-10T18:00:29.000000Z"))
    listen.comms_lib.initialize_proposals_cursor_if_missing(paths)
    n = listen._drain_proposals_once({}, paths)
    assert n == 0 and sent == [], "a stale backlog must not blast the channel"
    # A NEW proposal after init does deliver.
    with open(paths.proposals, "a") as f:
        f.write(json.dumps(_lesson("2026-07-08T10:00:00.000000Z")) + "\n")
    assert listen._drain_proposals_once({}, paths) == 1
