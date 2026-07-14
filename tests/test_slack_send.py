"""Tests for slack-send.py — the gated Slack send CLI.

Zero real egress: every test injects a fake `http` poster. The send-gate is the
security-critical path, so it gets the most coverage.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
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


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    return cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})


def _cfg(paths: cl.Paths, target="U123", allowed=("U123",)):
    paths.config.write_text(json.dumps({"slack": {
        "target": target, "allowed_targets": list(allowed)}}))


def _run(argv, http=None, env=None, paths=None):
    buf = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(buf):
        import contextlib
        with contextlib.redirect_stderr(err):
            rc = slack_send.main(argv, http=http, paths=paths,
                                 env=env if env is not None else {"SLACK_BOT_TOKEN": "xoxb-t"})
    out = buf.getvalue().strip()
    return rc, out, err.getvalue().strip()


# ─── the send-gate ──────────────────────────────────────────────────────────

def test_gate_blocks_non_allowlisted_target_no_http(paths: cl.Paths):
    _cfg(paths, target="U123", allowed=("U123",))

    def http(*a, **k):
        raise AssertionError("HTTP must NOT be called for a gated target")

    rc, out, err = _run(["--channel", "U999", "--text", "leak", "--kind", "action"],
                        http=http, paths=paths)
    assert rc == 1
    assert "send-gate" in err


def test_gate_blocks_even_the_configured_target_if_not_allowlisted(paths: cl.Paths):
    # target present but NOT in allowed_targets → still refused.
    _cfg(paths, target="U123", allowed=())

    def http(*a, **k):
        raise AssertionError("must not send")

    rc, _out, err = _run(["--channel", "U123", "--text", "x", "--kind", "action"],
                        http=http, paths=paths)
    assert rc == 1 and "send-gate" in err


def test_gate_allows_allowlisted_target(paths: cl.Paths):
    _cfg(paths)
    calls = []

    def http(token, method, payload):
        calls.append((method, payload))
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D999"}}
        return {"ok": True, "channel": payload["channel"], "ts": "1700.0001"}

    rc, out, _err = _run(["--channel", "U123", "--text", "hi", "--kind", "reply"],
                        http=http, paths=paths)
    assert rc == 0
    rec = json.loads(out)
    assert rec["channel"] == "D999" and rec["message_id"] == "1700.0001"
    assert calls[0][0] == "conversations.open" and calls[1][0] == "chat.postMessage"


def test_dash_target_uses_configured_default(paths: cl.Paths):
    _cfg(paths, target="Cchan", allowed=("Cchan",))

    def http(token, method, payload):
        # a C… channel is passed through — no conversations.open
        assert method == "chat.postMessage"
        return {"ok": True, "channel": payload["channel"], "ts": "1700.9"}

    rc, out, _err = _run(["--channel", "-", "--text", "hi", "--kind", "reply"],
                        http=http, paths=paths)
    assert rc == 0 and json.loads(out)["channel"] == "Cchan"


# ─── threading + ledger link ────────────────────────────────────────────────

def test_reply_to_sets_thread_ts(paths: cl.Paths):
    _cfg(paths, target="Cchan", allowed=("Cchan",))
    seen = {}

    def http(token, method, payload):
        seen[method] = payload
        return {"ok": True, "channel": payload["channel"], "ts": "1700.5"}

    rc, _out, _err = _run(["--channel", "Cchan", "--text", "re", "--kind", "reply",
                          "--reply-to", "1699.1"], http=http, paths=paths)
    assert rc == 0 and seen["chat.postMessage"]["thread_ts"] == "1699.1"


def test_ledger_key_records_thread(paths: cl.Paths):
    _cfg(paths, target="Cchan", allowed=("Cchan",))

    def http(token, method, payload):
        return {"ok": True, "channel": "Cchan", "ts": "1700.7"}

    rc, _out, _err = _run(["--channel", "Cchan", "--text", "x", "--kind", "action",
                          "--ledger-key", "assistant:close:ws:9"], http=http, paths=paths)
    assert rc == 0
    rec = cl.lookup_thread_by_msg_ts(paths, "1700.7")
    assert rec and rec["ledger_key"] == "assistant:close:ws:9"


# ─── mute + dry-run + errors ────────────────────────────────────────────────

def test_mute_suppresses_action_but_not_urgent(paths: cl.Paths):
    paths.config.write_text(json.dumps({
        "slack": {"target": "Cx", "allowed_targets": ["Cx"]},
        "mute_until_epoch": 9999999999}))

    def http(token, method, payload):
        raise AssertionError("muted action must not send")

    rc, out, _err = _run(["--channel", "Cx", "--text", "x", "--kind", "action"],
                        http=http, paths=paths, env={"SLACK_BOT_TOKEN": "t"})
    assert rc == 0 and json.loads(out)["muted"] is True

    # urgent bypasses the mute
    def http2(token, method, payload):
        return {"ok": True, "channel": "Cx", "ts": "1700.1"}

    rc2, out2, _ = _run(["--channel", "Cx", "--text", "u", "--kind", "urgent"],
                        http=http2, paths=paths)
    assert rc2 == 0 and json.loads(out2)["muted"] is False


def test_dry_run_no_http(paths: cl.Paths):
    _cfg(paths)

    def http(*a, **k):
        raise AssertionError("dry-run must not call http")

    rc, out, _err = _run(["--channel", "U123", "--text", "x", "--dry-run"],
                        http=http, paths=paths)
    assert rc == 0 and json.loads(out)["dry_run"] is True


def test_api_error_returns_2(paths: cl.Paths):
    _cfg(paths, target="Cx", allowed=("Cx",))

    def http(token, method, payload):
        raise RuntimeError("slack error: channel_not_found")

    rc, out, _err = _run(["--channel", "Cx", "--text", "x", "--kind", "action"],
                        http=http, paths=paths)
    assert rc == 2 and "channel_not_found" in json.loads(out)["error"]


def test_missing_token_non_dry_run_returns_1(paths: cl.Paths):
    _cfg(paths)
    rc, _out, err = _run(["--channel", "U123", "--text", "x"],
                        http=None, paths=paths, env={})
    assert rc == 1 and "SLACK_BOT_TOKEN" in err
