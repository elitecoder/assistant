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


# ─── reply_to_message threads the session provider (G3 integration) ──────────
#
# A persisted droid session must thread agent="droid" into every transcript-root
# / context call — the project folder searched for the submission, should_clear,
# and the clear_session delegation — else it reads the wrong root and never
# clears. Zero real cmux/network: every comms_session touchpoint is
# monkeypatched.


@pytest.fixture
def env_reply(monkeypatch):
    """Stub every cmux touchpoint reply_to_message can reach; record the agent
    threaded into each provider-aware call and what was typed."""
    rec = {"typed": [], "found": "/warm-t.jsonl", "submitted": True}

    def fake_submit(paths, surface_ref, text, marker, confirmed):
        rec["typed"].append((surface_ref, text, marker))
        return rec["submitted"] and confirmed()
    monkeypatch.setattr(listen.comms_session, "submit", fake_submit)

    def fake_project_dir(cwd, agent="claude"):
        rec["project_dir_agent"] = agent
        return Path("/proj")
    monkeypatch.setattr(listen.comms_session, "project_dir_for_cwd", fake_project_dir)
    monkeypatch.setattr(listen.comms_session, "transcript_has_submission",
                        lambda path, marker: path == rec.get("bound_hit"))
    monkeypatch.setattr(listen.comms_session, "find_submission",
                        lambda d, marker, since: rec["found"])

    def fake_should_clear(transcript, agent="claude"):
        rec["should_clear"] = (transcript, agent)
        return rec.get("_clear", True)
    monkeypatch.setattr(listen.comms_session, "should_clear", fake_should_clear)

    refreshed = {"ws_ref": "workspace:new", "surface_ref": "surface:new",
                 "cwd": "/cwd", "transcript_path": "/refreshed.jsonl", "agent": "droid"}

    def fake_clear(paths, sess, boot_prompt, agent="claude", log=None):
        rec["clear_agent"] = agent
        return refreshed
    monkeypatch.setattr(listen.comms_session, "clear_session", fake_clear)

    def fake_write(paths, ws, surface, cwd, transcript, **k):
        rec["wrote"] = transcript
    monkeypatch.setattr(listen.comms_session, "write_session", fake_write)
    monkeypatch.setattr(listen.comms_session, "read_session",
                        lambda paths: {"agent": "droid", "transcript_path": rec.get("wrote")})
    return rec, refreshed


def _droid_sess(transcript=None):
    return {"ws_ref": "workspace:5", "surface_ref": "surface:3", "cwd": "/cwd",
            "transcript_path": transcript, "agent": "droid"}


INBOUND = [{"channel": "C0", "text": "hi", "msg_ts": "1.1", "reply_to": None}]


def test_reply_threads_droid_agent_into_context_calls(env_reply, env_inbox):
    rec, _ = env_reply
    listen.reply_to_message(listen.comms_lib.Paths.from_env(), _droid_sess(), INBOUND)
    assert rec["project_dir_agent"] == "droid"
    assert rec["should_clear"] == ("/warm-t.jsonl", "droid")


def test_reply_types_the_header_and_waits_for_its_marker(env_reply, env_inbox):
    rec, _ = env_reply
    listen.reply_to_message(listen.comms_lib.Paths.from_env(), _droid_sess(), INBOUND)
    [(surface, text, marker)] = rec["typed"]
    assert surface == "surface:3"
    assert text == f"[slack channel=C0 msg_ts=1.1 send_cli={listen.SLACK_SEND}] hi"
    assert marker == "msg_ts=1.1"


def test_reply_delegates_to_clear_session_and_returns_refreshed(env_reply, env_inbox):
    rec, refreshed = env_reply
    out = listen.reply_to_message(listen.comms_lib.Paths.from_env(), _droid_sess(), INBOUND)
    assert rec["clear_agent"] == "droid"
    assert out == (True, refreshed)


def test_reply_rebinds_to_the_transcript_that_recorded_the_message(env_reply, env_inbox):
    """The recorded transcript was another session's (2026-09-28: ws:258 was
    bound to ws:256's file). The message lands elsewhere, and the session is
    rebound to where it landed."""
    rec, _ = env_reply
    rec["_clear"] = False
    delivered, out = listen.reply_to_message(listen.comms_lib.Paths.from_env(),
                                             _droid_sess("/wrong.jsonl"), INBOUND)
    assert delivered is True
    assert "clear_agent" not in rec, "should_clear False must not delegate to clear_session"
    assert rec["wrote"] == "/warm-t.jsonl"
    assert out["transcript_path"] == "/warm-t.jsonl"


def test_reply_keeps_the_bound_transcript_when_the_message_lands_there(env_reply, env_inbox):
    rec, _ = env_reply
    rec["_clear"] = False
    rec["bound_hit"] = "/bound.jsonl"
    rec["found"] = "/somewhere-else.jsonl"
    sess = _droid_sess("/bound.jsonl")
    delivered, out = listen.reply_to_message(listen.comms_lib.Paths.from_env(), sess, INBOUND)
    assert delivered is True and out is sess
    assert "wrote" not in rec, "no rebind when the bound transcript recorded it"


def test_reply_not_delivered_returns_false_and_leaves_the_session(env_reply, env_inbox):
    rec, _ = env_reply
    rec["submitted"] = False
    sess = _droid_sess()
    assert listen.reply_to_message(listen.comms_lib.Paths.from_env(), sess, INBOUND) == (False, sess)
    assert "should_clear" not in rec and "wrote" not in rec


def test_feed_text_combines_messages_that_piled_up():
    recs = [{"text": "Are you alive?", "msg_ts": "1.0"},
            {"text": "Pulse active now?", "msg_ts": "2.0"}]
    text = listen.feed_text(recs, "C0")
    assert text.startswith(f"[slack channel=C0 msg_ts=2.0 send_cli={listen.SLACK_SEND}] ")
    assert "2 messages arrived while your session was down" in text
    assert "(1) Are you alive? (2) Pulse active now?" in text


# ─── preflight doctor loader (regression: bare `import assistant_doctor` could
#     never resolve the hyphenated bin/assistant-doctor.py, so the preflight
#     silently ran its except-and-continue path on every startup) ─────────────

def test_load_doctor_resolves_hyphenated_module():
    doc = listen._load_doctor()
    # The real doctor module, usable by the preflight — not an ImportError.
    assert doc.__name__ == "assistant_doctor"
    assert hasattr(doc, "run_checks") and hasattr(doc, "FAIL")
    # It is the SAME module object the tests/doctor tests load (registered in
    # sys.modules under the importable name), so the preflight's contract holds.
    assert sys.modules.get("assistant_doctor") is doc


def test_load_doctor_runs_slack_checks():
    # The exact call the preflight makes — proves the loaded module is functional,
    # not just importable. (On the bug, this line raised ModuleNotFoundError.)
    doc = listen._load_doctor()
    checks = doc.run_checks(only="slack")
    assert isinstance(checks, list) and checks
    assert all(hasattr(c, "status") and hasattr(c, "name") for c in checks)


# ─── ensure_warm_session: respawn when the live session's model went stale ──────

def _stub_warm(monkeypatch, *, alive: bool | None, model_current: bool):
    """Stub the comms_session machinery ensure_warm_session drives; return a dict
    recording which lifecycle calls fired. alive=None means cmux didn't answer."""
    rec = {"closed": [], "cleared": False, "spawned": False}
    sess = {"ws_ref": "workspace:18", "agent": "claude",
            "model": "us.anthropic.claude-sonnet-4-6[1m]"}
    state = {True: listen.comms_session.ALIVE, False: listen.comms_session.GONE,
             None: listen.comms_session.UNKNOWN}[alive]
    monkeypatch.setattr(listen.comms_session, "read_session", lambda p: sess)
    monkeypatch.setattr(listen.comms_session, "workspace_state", lambda p, ref: state)
    monkeypatch.setattr(listen.comms_session, "warm_session_model_is_current",
                        lambda p, s: model_current)
    monkeypatch.setattr(listen.comms_session, "close_own_workspace",
                        lambda p, ref, log=None: rec["closed"].append(ref))
    def _clear(p):
        rec["cleared"] = True
    monkeypatch.setattr(listen.comms_session, "clear_session_registry", _clear)
    def _spawn(p, prompt, log=None):
        rec["spawned"] = True
        return {"ws_ref": "workspace:19", "agent": "claude", "model": "claude-sonnet-4-6[1m]"}
    monkeypatch.setattr(listen.comms_session, "spawn_session", _spawn)
    return rec, sess


def test_ensure_warm_session_keeps_alive_current_session(env_inbox, monkeypatch):
    """Alive AND model still current → reuse it, never respawn. Mutation probe:
    if the currency check were dropped, spawned would flip True."""
    rec, sess = _stub_warm(monkeypatch, alive=True, model_current=True)
    out = listen.ensure_warm_session(cl.Paths.from_env())
    assert out is sess
    assert rec["spawned"] is False and rec["closed"] == []


def test_ensure_warm_session_respawns_stale_on_inbound(env_inbox, monkeypatch):
    """On the INBOUND path (respawn_on_stale=True), an alive session whose model
    no longer matches the current backend (the 2026-09-16 Bedrock-after-toggle
    bug) is closed + respawned BEFORE the message is fed. Mutation probe: revert
    the currency gate and this fails (no respawn)."""
    rec, _ = _stub_warm(monkeypatch, alive=True, model_current=False)
    out = listen.ensure_warm_session(cl.Paths.from_env(), respawn_on_stale=True)
    assert rec["closed"] == ["workspace:18"], "stale session must be closed"
    assert rec["cleared"] is True
    assert rec["spawned"] is True, "a fresh session must be spawned on the current model"
    assert out["model"] == "claude-sonnet-4-6[1m]"


def test_ensure_warm_session_watchdog_keeps_stale_live_session(env_inbox, monkeypatch):
    """The WATCHDOG (respawn_on_stale=False) must NOT close a live-but-stale
    session — that could drop an in-flight reply and, returning 'alive', would
    dodge the watchdog backoff and churn cmux on a flapping resolver. A stale
    session still works on its old backend; only the inbound path respawns it.
    Mutation probe: drop the respawn_on_stale gate and this session gets closed."""
    rec, sess = _stub_warm(monkeypatch, alive=True, model_current=False)
    out = listen.ensure_warm_session(cl.Paths.from_env())  # watchdog default
    assert out is sess, "watchdog reuses the live session even when stale"
    assert rec["closed"] == [] and rec["spawned"] is False


def test_ensure_warm_session_respawns_when_gone(env_inbox, monkeypatch):
    """The pre-existing dead-session path still respawns, on either path. Model
    currency is not even consulted when the session is gone."""
    rec, _ = _stub_warm(monkeypatch, alive=False, model_current=True)
    listen.ensure_warm_session(cl.Paths.from_env())
    assert rec["closed"] == ["workspace:18"] and rec["spawned"] is True


def test_warm_session_left_alone_when_cmux_doesnt_answer(env_inbox, monkeypatch):
    """2026-09-27: a napping cmux refused connections, and each refused check
    closed a healthy warm session and spawned another onto the stalled cmux. A
    check cmux doesn't answer must keep the session and spawn nothing, even on
    the inbound path with a stale model. Mutation probe: treat UNKNOWN like
    GONE and the session is closed and respawned."""
    listen._cmux_silent_since = None
    rec, sess = _stub_warm(monkeypatch, alive=None, model_current=False)
    out, how = listen._warm_session(cl.Paths.from_env(), respawn_on_stale=True)
    assert out is sess and how == listen.SESSION_UNREACHABLE
    assert rec["closed"] == [] and rec["spawned"] is False and rec["cleared"] is False
    log = (cl.Paths.from_env().comms_dir / "comms-listen.log").read_text()
    assert log.count("cmux isn't answering") == 1
    listen._warm_session(cl.Paths.from_env())
    log = (cl.Paths.from_env().comms_dir / "comms-listen.log").read_text()
    assert log.count("cmux isn't answering") == 1, "one log line per silent episode"


def test_warm_session_logs_when_cmux_answers_again(env_inbox, monkeypatch):
    listen._cmux_silent_since = listen.time.time() - 42
    rec, sess = _stub_warm(monkeypatch, alive=True, model_current=True)
    out, how = listen._warm_session(cl.Paths.from_env())
    assert out is sess and how == listen.SESSION_ALIVE
    assert listen._cmux_silent_since is None
    assert "cmux answering again after" in (
        cl.Paths.from_env().comms_dir / "comms-listen.log").read_text()


def test_warm_session_force_respawn_replaces_a_live_session(env_inbox, monkeypatch):
    """A live session that didn't accept a typed message is replaced, so the
    queued message lands in a fresh one."""
    rec, _ = _stub_warm(monkeypatch, alive=True, model_current=True)
    out, how = listen._warm_session(cl.Paths.from_env(), force_respawn=True)
    assert rec["closed"] == ["workspace:18"] and rec["spawned"] is True
    assert how == listen.SESSION_SPAWNED and out["ws_ref"] == "workspace:19"


def test_warm_session_reports_a_failed_spawn(env_inbox, monkeypatch):
    rec, _ = _stub_warm(monkeypatch, alive=False, model_current=True)
    monkeypatch.setattr(listen.comms_session, "spawn_session", lambda p, prompt, log=None: None)
    assert listen._warm_session(cl.Paths.from_env()) == (None, listen.SESSION_NONE)
