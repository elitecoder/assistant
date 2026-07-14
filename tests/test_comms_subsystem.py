"""Tests for the in-process daemon Slack path: src/assistant/slack.py (client +
send-gate), conversation.py, config Slack fields, and CommsSubsystem.

Zero real egress: slack.send() takes an injected http poster."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from assistant import conversation, slack
from assistant.config import Config
from assistant.daemon import DaemonProcess
from assistant.subsystems.comms import CommsSubsystem


# ─── daemon wiring: comms is additive, gated on has_slack ───────────────────

def _daemon_config(tmp_path: Path) -> Config:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    return Config(home=home, assistant_dir=home / ".assistant",
                  repo=Path(__file__).resolve().parent.parent)


def test_daemon_omits_comms_when_slack_not_configured(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    d = DaemonProcess(_daemon_config(tmp_path), log=logging.getLogger("t"))
    assert "comms" not in {s.name for s in d.subsystems}


def test_daemon_wires_comms_when_slack_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    cfg = _daemon_config(tmp_path)
    object.__setattr__(cfg, "target", "U123")
    object.__setattr__(cfg, "allowed_targets", ("U123",))
    d = DaemonProcess(cfg, log=logging.getLogger("t"))
    assert "comms" in {s.name for s in d.subsystems}


# ─── slack client + gate ────────────────────────────────────────────────────

def test_slack_send_gate_blocks_unlisted():
    def http(*a, **k):
        raise AssertionError("gate must reject before any http")
    with pytest.raises(RuntimeError, match="send-gate"):
        slack.send("hi", "U999", token="t", allowed={"U123"}, http=http)


def test_every_slack_send_caller_passes_config_allowlist():
    """slack.send() gates against the caller-supplied `allowed` set, so a caller
    that passes a hand-built set (e.g. allowed={target}) would tautologically
    defeat the gate. Pin every in-package call site to pass the config's own
    allowlist — allowed=self.config.allowed_targets (or config.allowed_targets)
    — so the gate can't be neutered by a future caller. AST-checked so a literal
    set or a renamed source fails loudly."""
    import ast

    repo = Path(__file__).resolve().parent.parent
    callers = 0
    for py in (repo / "src").rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "send"
                    and isinstance(fn.value, ast.Name) and fn.value.id == "slack"):
                continue
            callers += 1
            kw = {k.arg: k.value for k in node.keywords}
            allowed = kw.get("allowed")
            # must be `<something>.allowed_targets` attribute access, NOT a
            # literal set/list/dict/call.
            ok = isinstance(allowed, ast.Attribute) and allowed.attr == "allowed_targets"
            assert ok, (
                f"{py.relative_to(repo)}:{getattr(node,'lineno','?')}: slack.send() "
                f"must pass allowed=<config>.allowed_targets, not a hand-built set "
                f"(got {ast.dump(allowed) if allowed else 'no allowed kwarg'})")
    assert callers >= 2, f"expected the 2 known slack.send callers, found {callers}"


def test_slack_send_resolves_user_then_posts():
    calls = []

    def http(token, method, payload):
        calls.append((method, payload))
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D1"}}
        return {"ok": True, "channel": payload["channel"], "ts": "1700.1"}

    res = slack.send("hi", "U123", token="t", allowed={"U123"}, http=http)
    assert res["ts"] == "1700.1" and res["channel"] == "D1"
    assert [c[0] for c in calls] == ["conversations.open", "chat.postMessage"]


def test_slack_send_channel_passthrough_no_open():
    def http(token, method, payload):
        assert method == "chat.postMessage"
        return {"ok": True, "channel": payload["channel"], "ts": "1700.2"}
    res = slack.send("hi", "Cchan", token="t", allowed={"Cchan"}, http=http)
    assert res["channel"] == "Cchan"


def test_slack_fmt_action_line_matches_comms_lib_shape():
    line = slack.fmt_action_line({"kind": "cleanup", "key": "k", "outcome": "verified",
                                  "verified_via": "screen_read"})
    assert "*[cleanup]* ok" in line and "(!)screen_read" in line


# ─── conversation (src-side) ────────────────────────────────────────────────

def test_conversation_roundtrip(tmp_path: Path):
    p = tmp_path / "conversation.jsonl"
    conversation.append_turn(p, "D42", "1.1", "in", "hi", clock=lambda: 1000)
    conversation.append_turn(p, "D42", "1.2", "out", "hey", kind="action", clock=lambda: 1001)
    rows = conversation.read_window(p, "D42", now=lambda: 1001)
    assert [r["text"] for r in rows] == ["hi", "hey"]
    assert rows[0]["channel"] == "D42"


def test_conversation_bad_direction(tmp_path: Path):
    with pytest.raises(ValueError):
        conversation.append_turn(tmp_path / "c.jsonl", "D", "1", "nope", "x")


# ─── config Slack fields ────────────────────────────────────────────────────

def _config(tmp_path: Path, **slack_block) -> Config:
    adir = tmp_path / ".assistant"
    adir.mkdir(parents=True, exist_ok=True)
    cfg_path = adir / "config.json"
    cfg_path.write_text(json.dumps({"slack": slack_block}))
    return Config.load(cfg_path, home=tmp_path, repo=Path.cwd())


def test_config_has_slack_requires_token_target_and_allowlist(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")
    cfg = _config(tmp_path, target="U123", allowed_targets=["U123"])
    assert cfg.has_slack
    assert cfg.bot_token == "xoxb-t"

    # target not in allowlist → not fully configured
    cfg2 = _config(tmp_path, target="U123", allowed_targets=[])
    assert not cfg2.has_slack

    # no token → not configured
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg3 = _config(tmp_path, target="U123", allowed_targets=["U123"])
    assert not cfg3.has_slack


def test_config_derived_comms_paths(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    cfg = _config(tmp_path, target="U1", allowed_targets=["U1"])
    assert cfg.conversation_path == cfg.assistant_dir / "comms" / "conversation.jsonl"
    assert cfg.ledger_path == cfg.assistant_dir / "actions-ledger.jsonl"
    assert cfg.ledger_cursor_path == cfg.assistant_dir / "comms" / "daemon-ledger.cursor"


# ─── CommsSubsystem: ledger broadcast ───────────────────────────────────────

class _FakeSlack:
    """Monkeypatched slack.send capture — records every send, returns a ts."""
    def __init__(self):
        self.sends = []

    def send(self, text, target, *, token, allowed, kind="reply", reply_to=None, http=None):
        if target not in set(allowed):
            raise RuntimeError("send-gate")
        self.sends.append({"text": text, "target": target, "kind": kind})
        return {"ok": True, "channel": target, "ts": f"1700.{len(self.sends)}"}


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")
    adir = tmp_path / ".assistant"
    (adir / "comms").mkdir(parents=True)
    cfg_path = adir / "config.json"
    cfg_path.write_text(json.dumps({"slack": {"target": "U123", "allowed_targets": ["U123"]}}))
    return Config.load(cfg_path, home=tmp_path, repo=Path.cwd())


def _make_subsystem(cfg, monkeypatch, send_enabled=True):
    fake = _FakeSlack()
    monkeypatch.setattr("assistant.subsystems.comms.slack.send", fake.send)
    stop = threading.Event()
    log = logging.getLogger("test.comms")
    sub = CommsSubsystem(cfg, stop, log, send_enabled=send_enabled)
    return sub, fake


def test_broadcast_sends_and_suppresses(cfg: Config, monkeypatch):
    sub, fake = _make_subsystem(cfg, monkeypatch)
    # a real action → sent
    sub._broadcast_entry({"kind": "cleanup", "key": "assistant:close:ws:5",
                          "outcome": "verified"})
    # routine noise → suppressed
    sub._broadcast_entry({"kind": "noop", "key": "noop:1", "outcome": "verified"})
    sub._broadcast_entry({"kind": "emit-card", "key": "card:1", "outcome": "verified"})
    sub._broadcast_entry({"kind": "self-update", "key": "self-update-skip", "outcome": "verified"})
    sub._broadcast_entry({"kind": "cleanup", "key": "skipped:1", "outcome": "skipped"})
    sub._broadcast_entry({"kind": "lesson-proposal", "key": "lesson-proposal:1", "outcome": "verified"})
    assert len(fake.sends) == 1
    assert fake.sends[0]["kind"] == "action"
    # broadcast mirrored into conversation.jsonl
    rows = conversation.read_window(cfg.conversation_path, "U123", now=time.time)
    assert len(rows) == 1 and rows[0]["direction"] == "out"


def test_broadcast_send_disabled_does_not_send(cfg: Config, monkeypatch):
    sub, fake = _make_subsystem(cfg, monkeypatch, send_enabled=False)
    sub._broadcast_entry({"kind": "cleanup", "key": "k", "outcome": "verified"})
    assert fake.sends == []


def test_heartbeat_pages_when_stale(cfg: Config, monkeypatch):
    sub, fake = _make_subsystem(cfg, monkeypatch)
    stale = int(time.time()) - 99999
    cfg.heartbeat_path.write_text(json.dumps({"last_pulse_ts": stale, "status": "ok",
                                              "ws_ref": "ws:1"}))
    sub._check_heartbeat()
    assert len(fake.sends) == 1 and fake.sends[0]["kind"] == "urgent"


def test_heartbeat_pages_on_bad_status(cfg: Config, monkeypatch):
    sub, fake = _make_subsystem(cfg, monkeypatch)
    fresh = int(time.time())
    cfg.heartbeat_path.write_text(json.dumps({"last_pulse_ts": fresh, "status": "frozen"}))
    sub._check_heartbeat()
    assert len(fake.sends) == 1


def test_heartbeat_healthy_no_page(cfg: Config, monkeypatch):
    sub, fake = _make_subsystem(cfg, monkeypatch)
    fresh = int(time.time())
    cfg.heartbeat_path.write_text(json.dumps({"last_pulse_ts": fresh, "status": "ok"}))
    sub._check_heartbeat()
    assert fake.sends == []


def test_heartbeat_dedup(cfg: Config, monkeypatch):
    sub, fake = _make_subsystem(cfg, monkeypatch)
    stale = int(time.time()) - 99999
    cfg.heartbeat_path.write_text(json.dumps({"last_pulse_ts": stale, "status": "ok"}))
    sub._check_heartbeat()
    sub._check_heartbeat()  # within dedup window
    assert len(fake.sends) == 1


def test_status_snapshot(cfg: Config, monkeypatch):
    sub, _fake = _make_subsystem(cfg, monkeypatch)
    st = sub.status()
    assert st["name"] == "comms" and st["send_enabled"] is True
