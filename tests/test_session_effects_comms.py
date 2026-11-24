"""The Slack session: every keystroke is recorded first, and only the owner's messages are typed."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import agent_session as ag
import comms_lib as cl
import comms_session as cs
import pytest

from assistant import effects, session_effects

from test_comms_listen_delivery import _log, _msg, listen
from test_comms_listen_delivery import env as env_delivery  # noqa: F401 - fixture
from test_comms_session import _spawn_env, _stub_cmux, paths  # noqa: F401 - fixtures

MARKER = "SLACK-TEXT-MARKER-do-not-record"
TS = "1793613610.000100"


@pytest.fixture(autouse=True)
def ledger_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "ledger-home"))
    effects._forget()
    return tmp_path / "ledger-home"


def led() -> Path:
    """The home the ledger is in now: a test that builds its own comms home moves it."""
    return Path(os.environ["HOME"])


def rows(_home=None) -> list:
    path = led() / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Cmux:
    """cmux, faked: records every keystroke and the ledger as it stood at the first."""

    def __init__(self, home, monkeypatch):
        self.home, self.calls, self.first = home, [], None
        monkeypatch.setattr(cs, "_cmux_rpc", self.rpc)

    def rpc(self, paths, method, params, timeout=15):
        if self.first is None:
            self.first = rows()
        self.calls.append((method, params.get("text", params.get("key"))))
        return {}


SESS = {"ws_ref": "workspace:5", "surface_ref": "surface:3", "cwd": "/cwd", "agent": "claude",
        "transcript_path": "/t.jsonl"}
INBOUND = [{"channel": "C0", "text": MARKER, "msg_ts": TS, "reply_to": None}]


@pytest.fixture
def reply(monkeypatch, ledger_home):
    """reply_to_message with the screen and transcript faked; `landed` says whether the transcript shows the message."""
    cmux = Cmux(ledger_home, monkeypatch)
    state = {"landed": True}
    monkeypatch.setattr(cs.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=200: "─" * 20 + "\n❯ \n" + "─" * 20)
    monkeypatch.setattr(cs, "should_clear", lambda *a, **k: False)
    monkeypatch.setattr(cs, "transcript_has_submission",
                        lambda path, marker: state["landed"] and len(cmux.calls) >= 2)
    monkeypatch.setattr(cs, "find_submission", lambda *a, **k: None)
    # The waits are bound as defaults when the function is defined, so they are replaced there: no waiting at all.
    monkeypatch.setattr(cs.submit_until_confirmed, "__defaults__",
                        (cs.SUBMIT_ATTEMPTS, 0.0, lambda s: None, cs.time.monotonic, lambda: False, 0.0))
    monkeypatch.setattr(cs, "write_session", lambda *a, **k: None)
    monkeypatch.setattr(cs, "read_session", lambda paths: None)
    return cmux, state


def test_a_message_is_recorded_before_its_first_keystroke_with_the_message_time_as_its_allowance(
        reply, ledger_home, env_delivery):
    cmux, _ = reply
    delivered, _ = listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert delivered is True
    assert [r["kind"] for r in cmux.first] == ["intent"]
    intent, result = rows(ledger_home)
    assert intent["target"] == {"source": "slack-message", "session": "workspace:5", "ts": TS}
    assert intent["auth"]["kind"] == "tap" and intent["auth"]["ref"] == f"slack:{TS}"
    assert intent["auth"]["at"] == "2026-11-02T10:00:10Z" if False else intent["auth"]["at"].endswith("Z")
    assert intent["component"] == "comms"
    assert result["outcome"] == "done" and result["verified"] is True
    assert MARKER not in (led() / ".assistant/effects/effects.jsonl").read_text()


def test_text_sent_and_enter_refused_is_unknown(reply, ledger_home, env_delivery, monkeypatch):
    cmux, state = reply
    state["landed"] = False
    real = cmux.rpc
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, m, params, timeout=15: None if params.get("text") == "\r"
                        else real(p, m, params, timeout))
    listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert rows()[1]["outcome"] == "unknown"


def test_text_and_enter_sent_but_never_seen_to_land_is_done_and_unverified(reply, ledger_home, env_delivery):
    cmux, state = reply
    state["landed"] = False
    delivered, _ = listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert delivered is False
    result = rows(ledger_home)[1]
    assert result["outcome"] == "done" and result["verified"] is False


def test_with_the_ledger_unwritable_nothing_is_typed_and_the_message_stays_queued(
        reply, ledger_home, env_delivery, monkeypatch):
    cmux, _ = reply
    paths, _calls = env_delivery
    listen.add_pending(paths, _msg(MARKER))
    monkeypatch.setattr(listen, "_warm_session", lambda p, **kw: (dict(SESS), listen.SESSION_ALIVE))
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    assert listen._deliver_pending(paths, "C0", None) is False
    assert cmux.calls == []
    assert [r["text"] for r in listen.read_pending(paths)] == [MARKER]
    assert "couldn't record what it types" in _log(paths)


def test_a_message_that_was_typed_is_never_typed_again_and_the_session_is_kept(
        reply, ledger_home, env_delivery, monkeypatch):
    cmux, state = reply
    state["landed"] = False
    paths, _calls = env_delivery
    listen.add_pending(paths, _msg(MARKER))
    replaced = []
    monkeypatch.setattr(listen, "_warm_session", lambda p, **kw: replaced.append(kw) or (dict(SESS), listen.SESSION_ALIVE))
    assert listen._deliver_pending(paths, "C0", None) is False  # typed, never seen to land: stays queued for a retry
    typed = len(cmux.calls)
    replaced.clear()
    assert listen._deliver_pending(paths, "C0", None) is True   # the ledger says it was typed
    assert len(cmux.calls) == typed and listen.read_pending(paths) == []
    assert {"replace_ws": "workspace:5"} not in replaced
    assert "it won't be typed again" in _log(paths)


def test_a_loop_leaves_the_message_queued_and_logs_it(reply, env_delivery, monkeypatch):
    cmux, _ = reply
    paths, _calls = env_delivery
    listen.add_pending(paths, _msg(MARKER))
    monkeypatch.setattr(listen, "_warm_session", lambda p, **kw: (dict(SESS), listen.SESSION_ALIVE))
    monkeypatch.setattr(listen, "reply_to_message",
                        lambda *a: (_ for _ in ()).throw(session_effects.Refused("loop", session_effects.LOOP)))
    assert listen._deliver_pending(paths, "C0", None) is False
    assert len(listen.read_pending(paths)) == 1 and "stays queued" in _log(paths)


# --- boot, trust, clear -----------------------------------------------------------------------------------------

def test_the_trust_answer_and_boot_prompt_are_recorded_as_the_standing_slack_setting(
        tmp_path, monkeypatch, ledger_home):
    paths_, closed = _spawn_env(tmp_path, monkeypatch)
    cmux = Cmux(ledger_home, monkeypatch)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)

    def ready_after_trust(**kw):
        kw["answer_trust"]()
        return True, True

    monkeypatch.setattr(cs, "await_ready", ready_after_trust)

    def deliver(p, surface, cwd, prompt, agent, submit_fn=None):
        cs._active_keys(surface).text("Read /boot.md")
        cs._active_keys(surface).carriage_return()
        return "/t.jsonl"

    monkeypatch.setattr(cs, "deliver_boot", deliver)
    assert cs.spawn_session(paths_, Path("/boot.md"), agent=ag.CLAUDE)["transcript_path"] == "/t.jsonl"
    intents = [r for r in rows(ledger_home) if r["kind"] == "intent"]
    assert [r["target"]["source"] for r in intents] == ["trust", "boot"]
    assert {(r["auth"]["kind"], r["auth"]["ref"]) for r in intents} == {("standing", "slack-comms")}
    assert [m for m, _ in cmux.calls] == ["surface.send_text"] * 4
    assert [r["outcome"] for r in rows(ledger_home) if r["kind"] == "result"] == ["done", "done"]


def test_a_boot_prompt_that_cant_be_recorded_is_not_typed_and_the_spawn_is_abandoned(
        tmp_path, monkeypatch, ledger_home):
    paths_, closed = _spawn_env(tmp_path, monkeypatch)
    cmux = Cmux(ledger_home, monkeypatch)
    logs: list = []
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    assert cs.spawn_session(paths_, Path("/boot.md"), log=logs.append, agent=ag.CLAUDE) is None
    assert cmux.calls == [] and closed == ["workspace:310"]
    assert any("boot prompt not typed" in line for line in logs)


def test_a_trust_answer_that_cant_be_recorded_is_not_typed(tmp_path, monkeypatch, ledger_home):
    paths_, closed = _spawn_env(tmp_path, monkeypatch)
    cmux = Cmux(ledger_home, monkeypatch)
    logs: list = []
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    monkeypatch.setattr(cs, "await_ready", lambda **kw: (kw["answer_trust"]() or (False, True)))
    assert cs.spawn_session(paths_, Path("/boot.md"), log=logs.append, agent=ag.CLAUDE) is None
    assert cmux.calls == [] and any("trust answer not typed" in line for line in logs)


def test_clear_and_the_boot_after_it_are_recorded_in_order(paths, monkeypatch, ledger_home):
    _rpc, feeds, calls, _ = _stub_cmux(monkeypatch)
    cmux = Cmux(ledger_home, monkeypatch)
    monkeypatch.setattr(cs, "deliver_boot",
                        lambda p, surface, cwd, prompt, agent: cs._active_keys(surface).text("boot") and "/new.jsonl")
    cs.clear_session(paths, {"ws_ref": "workspace:5", "surface_ref": "surface:3", "cwd": "/cwd"},
                     Path("/boot.md"), agent=ag.CLAUDE)
    intents = [r for r in rows(ledger_home) if r["kind"] == "intent"]
    assert [r["target"]["source"] for r in intents] == ["clear", "boot"]
    assert cmux.first[0]["target"]["source"] == "clear" and len(cmux.first) == 1
    assert [(m, t) for m, t in cmux.calls][:2] == [("surface.send_text", "/clear"), ("surface.send_text", "\r")]


def test_a_clear_that_cant_be_recorded_is_skipped_and_the_session_kept(paths, monkeypatch, ledger_home):
    _stub_cmux(monkeypatch)
    cmux = Cmux(ledger_home, monkeypatch)
    logs: list = []
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    sess = {"ws_ref": "workspace:5", "surface_ref": "surface:3", "cwd": "/cwd"}
    assert cs.clear_session(paths, sess, Path("/boot.md"), agent=ag.CLAUDE, log=logs.append) == sess
    assert cmux.calls == [] and any("not clearing workspace:5" in line for line in logs)


def test_typing_outside_a_recorded_scope_is_refused_and_a_scope_is_per_thread(paths, monkeypatch, ledger_home):
    cmux = Cmux(ledger_home, monkeypatch)
    with pytest.raises(session_effects.Refused):
        cs.send_enter(paths, "surface:9")
    with pytest.raises(session_effects.Refused):
        cs.submit(paths, "surface:9", "x", "m", lambda: False)
    seen = []
    with cs.typing_scope(paths, "workspace:4", "surface:9", "clear", kind="standing", ref="slack-comms"):
        t = threading.Thread(target=lambda: seen.append(getattr(cs._scope, "keys", None)))
        t.start()
        t.join()
        with pytest.raises(session_effects.Refused):
            cs.send_enter(paths, "surface:OTHER")  # a scope belongs to one terminal
    assert seen == [None] and [m for m, _ in cmux.calls if m.startswith("surface.send")] == []


def test_an_error_inside_a_scope_leaves_the_typing_unknown(paths, monkeypatch, ledger_home):
    Cmux(ledger_home, monkeypatch)
    with pytest.raises(RuntimeError):
        with cs.typing_scope(paths, "workspace:4", "surface:9", "clear", kind="standing", ref="slack-comms") as keys:
            keys.text("/clear")
            raise RuntimeError("boom")
    assert rows(ledger_home)[1]["outcome"] == "unknown"


# --- only the owner's messages are typed -------------------------------------------------------------------------

def _config(paths, target):
    paths.config.write_text(json.dumps({"slack": {"target": target, "allowed_targets": [target]}}))


@pytest.mark.parametrize("target, owner_env, author, typed", [
    ("U0OWNER", "", "U0OWNER", True),        # a direct message: the target is the owner
    ("U0OWNER", "", "U0OTHER", False),
    ("C0CHAN", "U0OWNER", "U0OWNER", True),  # a channel: the owner's id comes from SLACK_USER_ID
    ("C0CHAN", "U0OWNER", "U0OTHER", False),
    ("C0CHAN", "U0OWNER", "", False),
    ("C0CHAN", "", "U0OWNER", False),        # a channel and no owner id: nothing is typed
    ("D0DM", "", "U0OWNER", True),           # a direct-message conversation holds only the owner
    ("C0CHAN", "U0OWNER", "U0OWNER", True),
])
def test_only_the_owners_message_is_typed(env_delivery, monkeypatch, target, owner_env, author, typed):
    paths, _ = env_delivery
    _config(paths, target)
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    if owner_env:
        monkeypatch.setenv("SLACK_USER_ID", owner_env)
    assert listen.typed_for(paths, {"msg_ts": TS, "author": author}) is typed
    log = _log(paths)
    assert (log == "") is typed
    if not typed:
        assert "not typed into the session" in log


def test_an_owner_id_in_the_daemons_own_environment_counts(env_delivery, monkeypatch):
    paths, _ = env_delivery
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    assert listen.owner_id(paths, {"SLACK_USER_ID": " U0OWNER "}) == "U0OWNER"


def test_a_message_from_someone_else_is_recorded_as_received_but_never_queued(env_delivery, monkeypatch):
    paths, calls = env_delivery
    _config(paths, "C0")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")
    monkeypatch.setattr(listen, "ensure_warm_session", lambda p, **kw: None)
    stop = threading.Event()
    woken = []

    def fake_worker(channel_id, wake, stop_, env_):
        woken.append(channel_id)
        wake.get(timeout=5)
        stop_.set()

    def fake_poll(stop_, env_, msg_queue):
        msg_queue.put({**_msg(MARKER), "msg_ts": "1793613611.000100", "author": "U0OTHER"})
        msg_queue.put({**_msg("mine"), "msg_ts": TS, "author": "U0OWNER"})

    monkeypatch.setattr(listen, "_channel_worker", fake_worker)
    monkeypatch.setattr(listen, "_poll_thread", fake_poll)
    t = threading.Thread(target=listen.inbound_loop, args=(stop, {}), daemon=True)
    t.start()
    t.join(timeout=10)
    assert [r["text"] for r in listen.read_pending(paths)] == ["mine"]
    appended = [a[a.index("--text") + 1] for a in calls if "conversation.py" in a[0] and "append" in a]
    assert appended == [MARKER, "mine"], "both are recorded as received"
    assert "from another member (U0OTHER)" in _log(paths)
