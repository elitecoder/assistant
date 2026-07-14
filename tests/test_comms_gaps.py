"""Gap-closing tests — the pure logic surfaced by a coverage audit before push.

Targets the untested-but-cheaply-testable paths: comms-listen's inbound
reply_to_message flow + helpers, CLI error/exit paths, and comms_lib guard
branches (malformed cursors, bad JSON, bedrock env parsing). The true I/O seams
(urllib _real_post, threaded daemon loops, live cmux in comms_session) stay
validated live, not here — injecting a fake at those boundaries is what the
other suites already do.
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
    # Load the REAL module. comms_session imports cleanly without cmux (it only
    # touches cmux at call-time), so no stub is needed — and stubbing it into
    # sys.modules would leak into test_comms_session.py's `import comms_session`.
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(Path(__file__).resolve().parent.parent / "bin" / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    return cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})


# ─── comms_lib guard branches ───────────────────────────────────────────────

def test_ledger_cursor_bad_value_falls_back_to_zero(paths: cl.Paths):
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    paths.cursor.write_text("not-an-int")
    assert cl.read_ledger_cursor(paths) == 0


def test_slack_cursor_missing_is_zero(paths: cl.Paths):
    assert cl.read_slack_cursor(paths) == "0"


def test_ts_float_handles_garbage():
    assert cl._ts_float("1700.5") == 1700.5
    assert cl._ts_float(None) == 0.0
    assert cl._ts_float("garbage") == 0.0


def test_read_new_ledger_lines_missing_file(paths: cl.Paths):
    assert cl.read_new_ledger_lines(paths) == []


def test_lookup_thread_skips_malformed_lines(paths: cl.Paths):
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    paths.threads.write_text('{"msg_ts":"1.1","ledger_key":"k","channel":"C"}\n'
                             'not-json\n'
                             '{"msg_ts":"1.2","ledger_key":"k","channel":"C"}\n')
    assert len(cl.lookup_thread_by_ledger_key(paths, "k")) == 2
    assert cl.lookup_thread_by_msg_ts(paths, "1.1") is not None


def test_conversation_window_skips_malformed(paths: cl.Paths):
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    paths.conversation.write_text(
        json.dumps({"channel": "C", "epoch": 100, "text": "ok", "direction": "in"}) + "\n"
        + "garbage\n")
    rows = cl.read_conversation_window(paths, "C", now=lambda: 100)
    assert [r["text"] for r in rows] == ["ok"]


def test_read_comms_heartbeat_bad_json(paths: cl.Paths):
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    paths.daemon_hb.write_text("{bad")
    assert cl.read_comms_heartbeat(paths) is None


def test_write_then_read_comms_heartbeat(paths: cl.Paths):
    cl.write_comms_heartbeat(paths, status="active", pulse_idx=3, note="x", clock=lambda: 5)
    hb = cl.read_comms_heartbeat(paths)
    assert hb["status"] == "active" and hb["pulse_idx"] == 3 and hb["note"] == "x"


def test_send_notification_missing_config_returns_false(paths: cl.Paths):
    # no config.json → Config.load raises SystemExit → send_notification False
    assert cl.send_notification("x", paths.config, Path("/repo/bin")) is False


def test_load_bedrock_env_parses_exports(tmp_path: Path):
    zp = tmp_path / ".zprofile"
    zp.write_text(
        'export AWS_REGION="us-east-1"\n'
        "export SLACK_BOT_TOKEN='xoxb-abc'\n"
        "export IRRELEVANT=nope\n"
        "not an export line\n")
    env = cl.load_bedrock_env(home=tmp_path)
    assert env["AWS_REGION"] == "us-east-1"
    assert env["SLACK_BOT_TOKEN"] == "xoxb-abc"
    assert "IRRELEVANT" not in env


def test_load_bedrock_env_no_zprofile(tmp_path: Path):
    assert cl.load_bedrock_env(home=tmp_path) == {}


def test_fmt_workspace_signal_pattern_match_label():
    body = cl.fmt_workspace_signal({"ws_ref": "ws:2", "signal_type": "pattern_match",
                                    "pattern_matched": "BUILD FAILED"})
    assert "hit a watched signal" in body and "BUILD FAILED" in body


# ─── slack-send CLI error paths ─────────────────────────────────────────────

slack_send = _load("slack_send", "slack-send.py")


def test_send_no_target_and_none_configured_returns_1(paths: cl.Paths):
    paths.config.write_text(json.dumps({"slack": {"allowed_targets": []}}))  # no target
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = slack_send.main(["--channel", "-", "--text", "x"], paths=paths,
                             env={"SLACK_BOT_TOKEN": "t"})
    assert rc == 1 and "no target" in err.getvalue()


# ─── slack-poll CLI error paths ─────────────────────────────────────────────

slack_poll = _load("slack_poll", "slack-poll.py")


def test_poll_no_token_returns_1(paths: cl.Paths):
    paths.config.write_text(json.dumps({"slack": {"target": "C0", "allowed_targets": ["C0"]}}))
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = slack_poll.main([], paths=paths, env={})  # no SLACK_BOT_TOKEN
    assert rc == 1 and "SLACK_BOT_TOKEN" in err.getvalue()


def test_poll_fetch_error_returns_1(paths: cl.Paths):
    paths.config.write_text(json.dumps({"slack": {"target": "C0", "allowed_targets": ["C0"]}}))

    def http(token, method, params):
        raise RuntimeError("slack error: ratelimited")

    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = slack_poll.main([], http=http, paths=paths, env={"SLACK_BOT_TOKEN": "t"})
    assert rc == 1 and "ratelimited" in err.getvalue()


# ─── comms-listen inbound reply flow (the core, previously untested) ─────────

def test_reply_to_message_records_inbound_and_feeds_session(paths: cl.Paths, monkeypatch):
    listen = _load("comms_listen", "comms-listen.py")
    cs = sys.modules["comms_session"]

    # monkeypatch.setattr auto-restores after the test, so the real comms_session
    # module is left pristine for test_comms_session.py (no cross-file leak).
    fed = {}
    monkeypatch.setattr(cs, "newest_transcript", lambda cwd: "/tmp/fake.jsonl")
    monkeypatch.setattr(cs, "transcript_line_count", lambda t: 0)
    monkeypatch.setattr(cs, "should_clear", lambda t: False)
    monkeypatch.setattr(cs, "feed", lambda paths, surface, text: fed.setdefault("feed", text))
    monkeypatch.setattr(cs, "write_session", lambda *a, **k: None)
    monkeypatch.setattr(cs, "read_session", lambda paths: None)

    calls = []
    monkeypatch.setattr(listen, "cli", lambda argv, timeout=30, env=None: (calls.append(argv) or (0, "[]", "")))
    monkeypatch.setattr(listen.time, "sleep", lambda s: None)
    monkeypatch.setattr(listen, "REPLY_WAIT_SEC", 0)

    sess = {"ws_ref": "workspace:1", "surface_ref": "surface:1",
            "cwd": "/tmp", "transcript_path": "/tmp/fake.jsonl"}
    rec = {"channel": "C0", "text": "how's the fleet?", "msg_ts": "100.1", "reply_to": None}
    listen.reply_to_message(paths, sess, rec)

    # inbound turn recorded via conversation.py append
    assert any("conversation.py" in a[0] and "append" in a for a in calls), calls
    # message fed to the warm session with a flat (no thread_root) slack header
    assert "how's the fleet?" in fed["feed"]
    assert "slack channel=C0" in fed["feed"] and "send_cli=" in fed["feed"]
    assert "thread_root" not in fed["feed"]  # 1:1 flat model


def test_suppress_reason_matrix():
    listen = _load("comms_listen", "comms-listen.py")
    supp = listen._suppress_reason
    # suppressed
    assert supp({"outcome": "skipped", "kind": "cleanup", "key": "k"})
    assert supp({"kind": "noop", "key": "workspace:1-active"})
    assert supp({"kind": "emit-card", "key": "workspace:2-needs_user"})
    assert supp({"kind": "self-update", "key": "self-update-skip-p1"})
    assert supp({"kind": "lesson-proposal", "key": "lesson-proposal:1"})
    assert supp({"kind": "x", "key": "lesson-proposal-abc"})
    # NOT suppressed — real actionable events, incl. self-update FAILURES.
    assert supp({"kind": "cleanup", "key": "assistant:close:ws:5", "outcome": "verified"}) is None
    assert supp({"kind": "self-update", "key": "self-update-fail-p8002",
                 "outcome": "failed"}) is None  # the recurring fetch-fail SHOULD surface


def test_send_args_and_target_helpers(paths: cl.Paths, monkeypatch):
    listen = _load("comms_listen", "comms-listen.py")
    argv = listen._send_args("body text", "action", "C0", "assistant:key:1")
    assert "--channel" in argv and "C0" in argv
    assert "--kind" in argv and "action" in argv
    assert "--ledger-key" in argv and "assistant:key:1" in argv
    # _target reads config
    paths.config.write_text(json.dumps({"slack": {"target": "C9", "allowed_targets": ["C9"]}}))
    monkeypatch.setattr(listen.comms_lib, "Paths", type("P", (), {"from_env": staticmethod(lambda: paths)}))
    assert listen._target(paths) == "C9"
