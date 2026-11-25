"""The Slack session: every keystroke is recorded first, and only the owner's messages are typed once Assistant
knows who the owner is."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import agent_session as ag
import comms_lib as cl
import comms_session as cs
import pytest

from assistant import effects, session_effects

from test_comms_listen_delivery import _log, _msg, _sends, listen
from test_comms_listen_delivery import env as env_delivery  # noqa: F401 - fixture
from test_comms_session import _spawn_env, _stub_cmux, paths  # noqa: F401 - fixtures

OTHER = ("I only take instructions from <@U0OWNER>, so I haven't acted on this message. If that isn't you, "
         "SLACK_USER_ID is set to the wrong member ID: put your own in ~/.zprofile and run "
         "`launchctl kickstart -k gui/$UID/com.assistant.assistant-comms`.")
MARKER = "SLACK-TEXT-MARKER-do-not-record"
TS = "1793613610.000100"
WS = "workspace:1000010005"   # cmux numbers its refs from 1000010000
WS2 = "workspace:1000010006"


@pytest.fixture(autouse=True)
def ledger_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "ledger-home"))
    effects._forget()
    return tmp_path / "ledger-home"


def led() -> Path:
    """The home the ledger is in now: a test that builds its own comms home moves it."""
    return Path(os.environ["HOME"])


def rows() -> list:
    path = led() / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Cmux:
    """cmux, faked: records every keystroke and the ledger as it stood at the first."""

    def __init__(self, monkeypatch, rc=0, refuse_text=()):
        self.calls, self.first, self.rc, self.refuse_text = [], None, rc, refuse_text
        monkeypatch.setattr(cs, "_cmux_rpc", self.rpc)

    def rpc(self, paths, method, params, timeout=15):
        if method.startswith("surface.send"):
            if self.first is None:
                self.first = rows()
            self.calls.append((method, params.get("text", params.get("key"))))
            cl._last.result = (self.rc, "timeout after 15s" if self.rc == -1 else "")
            return None if self.rc or params.get("text") in self.refuse_text else {}
        return {}

    def typed(self) -> list:
        return [text for _method, text in self.calls]


SESS = {"ws_ref": WS, "surface_ref": "surface:3", "cwd": "/cwd", "agent": "claude", "transcript_path": "/t.jsonl"}
INBOUND = [{"channel": "C0", "text": MARKER, "msg_ts": TS, "reply_to": None, "author": "UOWNER"}]


@pytest.fixture
def reply(monkeypatch, env_delivery):
    """reply_to_message with the screen and transcript faked; `state` says whether the transcript shows the message
    and whether the prompt box still holds it."""
    cmux = Cmux(monkeypatch)
    state = {"landed": True, "box": ""}
    monkeypatch.setattr(cs.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(cs, "_surface_read_text",
                        lambda p, ref, lines=200: "─" * 20 + f"\n❯ {state['box']}\n" + "─" * 20)
    monkeypatch.setattr(cs, "should_clear", lambda *a, **k: False)
    monkeypatch.setattr(cs, "transcript_has_submission",
                        lambda path, marker: state["landed"] and len(cmux.calls) >= 2)
    monkeypatch.setattr(cs, "find_submission", lambda *a, **k: None)
    # The waits are bound as defaults when the function is defined, so they are replaced there: no waiting at all.
    monkeypatch.setattr(cs.submit_until_confirmed, "__defaults__",
                        (cs.SUBMIT_ATTEMPTS, 0.0, lambda s: None, cs.time.monotonic, lambda: False, 0.0, False))
    monkeypatch.setattr(cs, "write_session", lambda *a, **k: None)
    monkeypatch.setattr(cs, "read_session", lambda paths: None)
    return cmux, state


def pending(paths, **kw):
    rec = {**_msg(MARKER), **kw}
    listen.add_pending(paths, rec)
    return rec


def serve(monkeypatch, sessions=None, how=None):
    seen = []
    monkeypatch.setattr(listen, "_warm_session",
                        lambda p, **kw: seen.append(kw) or (dict(SESS), how or listen.SESSION_ALIVE))
    return seen


def replies(calls) -> list:
    """(text, thread) for each Slack send."""
    out = []
    for argv in calls:
        if "slack-send.py" in argv[0]:
            out.append((argv[argv.index("--text") + 1], argv[argv.index("--reply-to") + 1]
                        if "--reply-to" in argv else None))
    return out


# --- a message is recorded before it is typed -------------------------------------------------------------------

def test_a_message_is_recorded_before_its_first_keystroke_with_the_message_time_as_its_allowance(reply, env_delivery):
    cmux, _ = reply
    delivered, _ = listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert delivered is True
    assert [r["kind"] for r in cmux.first] == ["intent"]
    intent, result = rows()
    assert intent["target"] == {"source": "slack-message", "session": WS, "ts": TS}
    assert intent["auth"] == {"kind": "tap", "ref": f"slack:{TS}", "at": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(TS)))}
    assert intent["component"] == "comms"
    assert result["outcome"] == "done" and result["verified"] is True
    assert MARKER not in (led() / ".assistant/effects/effects.jsonl").read_text()


def test_text_and_enter_sent_but_never_seen_to_land_is_unknown_and_unverified(reply, env_delivery):
    cmux, state = reply
    state["landed"] = False
    delivered, _ = listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert delivered is False
    result = rows()[1]
    assert result["outcome"] == "unknown" and result["verified"] is False


def test_text_sent_and_enter_refused_is_unknown(reply, env_delivery, monkeypatch):
    cmux, state = reply
    state["landed"] = False
    cmux.refuse_text = ("\r",)
    listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert rows()[1]["outcome"] == "unknown"


def test_a_timeout_while_typing_is_unknown_not_failed(reply, env_delivery):
    cmux, state = reply
    cmux.rc = -1
    state["landed"] = False
    listen.reply_to_message(cl.Paths.from_env(), dict(SESS), INBOUND)
    assert rows()[1]["outcome"] == "unknown" and rows()[1]["error"] == "timeout"


def test_an_exit_status_of_zero_is_an_answer_whatever_the_body(paths, monkeypatch):
    sent = []
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, m, params, timeout=15: (
        sent.append(m) or setattr(cl._last, "result", (0, "")) or None))  # it ran, and printed nothing
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        assert keys.text("x") is True and keys.carriage_return() is True
    assert sent == ["surface.send_text"] * 2 and rows()[1]["outcome"] == "done"


# --- a refused message is kept, or dropped with a word to the owner ---------------------------------------------

def test_with_the_ledger_unwritable_nothing_is_typed_the_message_stays_and_the_back_soon_notice_still_goes(
        reply, env_delivery, monkeypatch):
    cmux, _ = reply
    paths, calls = env_delivery
    rec = pending(paths, msg_ts=f"{time.time() - 600:.6f}")
    serve(monkeypatch)
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    assert listen._deliver_pending(paths, "C0", None) is False
    assert cmux.calls == [] and [r["text"] for r in listen.read_pending(paths)] == [MARKER]
    assert "couldn't record what it types" in _log(paths) and rec["msg_ts"] in _log(paths)
    assert _sends(calls) == ["I can't save my record of what I type into the session, so I haven't passed your message on "
                             "yet. I'll try again every few minutes. Check that the disk isn't full."]


def test_a_message_dated_ahead_of_this_macs_clock_stays_queued(reply, env_delivery, monkeypatch):
    cmux, _ = reply
    paths, calls = env_delivery
    pending(paths, msg_ts=f"{time.time() + 3600:.6f}")
    serve(monkeypatch)
    assert listen._deliver_pending(paths, "C0", None) is False
    assert cmux.calls == [] and len(listen.read_pending(paths)) == 1 and "clock" in _log(paths)


def test_a_message_typed_but_never_seen_has_only_its_enter_pressed_again_while_it_sits_in_the_box(
        reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    rec = pending(paths)
    state["landed"] = False
    serve(monkeypatch)
    assert listen._deliver_pending(paths, "C0", None) is False  # typed, Enter pressed, never seen to land
    assert len(cmux.typed()) == 2 and cmux.typed()[0].startswith("[slack channel=") and cmux.typed()[1] == "\r"
    state["box"] = f"[slack channel=C0 msg_ts={rec['msg_ts']}] {MARKER}"
    assert listen._deliver_pending(paths, "C0", None) is False  # its Enter again, and still not seen
    assert cmux.typed()[2:] == ["\r"] * cs.SUBMIT_ATTEMPTS      # and nothing else: its text only once
    assert sum(1 for t in cmux.typed() if t.startswith("[slack channel=")) == 1
    intents = [r["target"] for r in rows() if r["kind"] == "intent"]
    assert intents == [{"source": "slack-message", "session": WS, "ts": rec["msg_ts"]},
                       {"source": "submit", "session": WS, "ts": rec["msg_ts"]}]
    assert [r["outcome"] for r in rows() if r["kind"] == "result"] == ["unknown", "unknown"]


@pytest.mark.parametrize("box", ["rm -rf ~/dev half typed by the owner", "[Pasted text #1 +4 lines]", ""])
def test_enter_is_not_pressed_on_a_box_that_no_longer_shows_this_message(reply, env_delivery, monkeypatch, box):
    """The owner started typing between the check and the Enter: their draft must never be submitted."""
    cmux, state = reply
    paths, calls = env_delivery
    rec = pending(paths)
    state["landed"] = False
    serve(monkeypatch)
    listen._deliver_pending(paths, "C0", None)               # typed, never seen
    typed = len(cmux.typed())
    boxes = iter([f"msg_ts={rec['msg_ts']}"])                # the first read (the check) still shows the message
    state["box"] = None
    monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=200: "─" * 20 + "\n❯ " + next(boxes, box) + "\n" + "─" * 20)
    assert listen._deliver_pending(paths, "C0", None) is True      # dropped, with a word to the owner
    assert cmux.typed()[typed:] == [] and listen.read_pending(paths) == []
    assert [r["outcome"] for r in rows() if r["kind"] == "result"][-1] == "failed"  # nothing was sent
    assert replies(calls)[-1][0].startswith("I couldn't confirm your message")


def test_in_the_enter_only_mode_nothing_is_typed_and_a_collapsed_paste_is_not_this_message(monkeypatch):
    pressed, typed = [], []
    box = {"text": "[Pasted text #1 +4 lines]"}
    got = cs.submit_until_confirmed(
        send_text=lambda: typed.append(1), press_enter=lambda: pressed.append(1), read_box=lambda: box["text"],
        confirmed=lambda: False, marker="msg_ts=5", sleep=lambda s: None, enter_only=True)
    assert got is False and pressed == [] and typed == []
    box["text"] = "msg_ts=5 hello"
    got = cs.submit_until_confirmed(
        send_text=lambda: typed.append(1), press_enter=lambda: pressed.append(1), read_box=lambda: box["text"],
        confirmed=lambda: False, marker="msg_ts=5", attempts=2, wait_sec=0, sleep=lambda s: None, enter_only=True)
    assert got is False and len(pressed) == 2 and typed == []  # Enter again only while the box shows the message
    reads = iter(["msg_ts=5 hello"])  # the first look shows the message; then something else is in the box
    pressed.clear()
    cs.submit_until_confirmed(
        send_text=lambda: typed.append(1), press_enter=lambda: pressed.append(1),
        read_box=lambda: next(reads, "[Pasted text #1 +4 lines]"), confirmed=lambda: False, marker="msg_ts=5",
        attempts=3, wait_sec=0, sleep=lambda s: None, enter_only=True)
    assert len(pressed) == 1 and typed == []  # a collapsed paste is not this message, so no second Enter
    box["text"] = "[Pasted text #1 +4 lines]"
    pressed.clear()
    cs.submit_until_confirmed(
        send_text=lambda: typed.append(1), press_enter=lambda: pressed.append(1), read_box=lambda: box["text"],
        confirmed=lambda: False, marker="msg_ts=5", attempts=2, wait_sec=0, sleep=lambda s: None, enter_only=False)
    assert typed == [1]  # the normal mode still types, as before


def test_enter_only_retries_of_one_message_stop_at_the_loop_limit_and_the_owner_is_told_but_a_new_message_is_typed(
        reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    rec = pending(paths)
    state["landed"] = False
    serve(monkeypatch)
    state["box"] = f"[slack channel=C0 msg_ts={rec['msg_ts']}] {MARKER}"  # it stays in the box, never submitted
    outcomes = [listen._deliver_pending(paths, "C0", None) for _ in range(2 + session_effects.LOOP_MAX)]
    assert outcomes == [False] * (1 + session_effects.LOOP_MAX) + [True]  # typed, three Enter retries, then dropped
    submits = [r for r in rows() if r["kind"] == "intent" and r["target"]["source"] == "submit"]
    assert len(submits) == session_effects.LOOP_MAX
    assert listen.read_pending(paths) == []
    assert replies(calls)[-1][0].startswith("I couldn't confirm your message") and replies(calls)[-1][1] == rec["msg_ts"]
    typed = len(cmux.typed())
    state["box"] = ""  # (and still nothing in the transcript: only whether it is typed matters here)
    new = pending(paths, text="again", msg_ts=f"{time.time():.6f}")  # the owner sends something new
    listen._deliver_pending(paths, "C0", None)
    assert any(t.startswith("[slack channel=") and "again" in t for t in cmux.typed()[typed:])  # typed at once


def test_an_enter_only_retry_that_cant_be_recorded_leaves_the_message_queued_not_dropped(
        reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    rec = pending(paths)
    state["landed"] = False
    serve(monkeypatch)
    listen._deliver_pending(paths, "C0", None)  # typed, never seen
    state["box"] = f"[slack channel=C0 msg_ts={rec['msg_ts']}] {MARKER}"
    real = effects.begin

    def begin(cls, target, *a, **k):
        if target.get("source") == "submit":
            raise effects.Unrecorded("disk full")
        return real(cls, target, *a, **k)

    monkeypatch.setattr(effects, "begin", begin)
    assert listen._deliver_pending(paths, "C0", None) is False
    assert [r["text"] for r in listen.read_pending(paths)] == [MARKER] and replies(calls) == []


def test_a_message_that_landed_since_is_delivered_without_typing_anything(reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    pending(paths)
    state["landed"] = False
    serve(monkeypatch)
    listen._deliver_pending(paths, "C0", None)
    state["landed"] = True
    typed = len(cmux.calls)
    monkeypatch.setattr(cs, "transcript_has_submission", lambda path, marker: True)
    assert listen._deliver_pending(paths, "C0", None) is True
    assert len(cmux.calls) == typed and listen.read_pending(paths) == []
    assert replies(calls) == [], "it arrived, so nobody is told it was dropped"


def test_a_message_that_is_neither_confirmed_nor_in_the_box_is_dropped_and_the_owner_is_told_once(
        reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    rec = pending(paths)
    state["landed"] = False
    seen = serve(monkeypatch)
    listen._deliver_pending(paths, "C0", None)   # typed, never seen
    seen.clear()
    typed = len(cmux.calls)
    assert listen._deliver_pending(paths, "C0", None) is True   # nothing more can be done: it leaves the queue
    assert len(cmux.calls) == typed and listen.read_pending(paths) == []
    assert {"replace_ws": WS} not in seen, "the session did nothing wrong"
    expected = (f"I couldn't confirm your message (sent {listen._clock(rec['msg_ts'])}) reached the session, and I "
                "won't type it twice. Check the session, or send it again.")
    assert replies(calls) == [(expected, rec["msg_ts"])]
    assert f"Slack message {rec['msg_ts']} in C0: your message was dropped, and won't be typed again." in _log(paths)


def test_a_message_that_cant_be_described_is_dropped_with_its_own_words(reply, env_delivery, monkeypatch):
    paths, calls = env_delivery
    rec = pending(paths)
    serve(monkeypatch)
    monkeypatch.setattr(listen, "reply_to_message",
                        lambda *a: (_ for _ in ()).throw(session_effects.Refused("invalid", session_effects.INVALID)))
    assert listen._deliver_pending(paths, "C0", None) is True
    [(text, thread)] = replies(calls)
    assert text.startswith("I couldn't pass your message") and thread == rec["msg_ts"]


def test_a_loop_leaves_the_message_queued_and_says_when_it_will_be_tried_again(reply, env_delivery, monkeypatch):
    paths, calls = env_delivery
    pending(paths)
    serve(monkeypatch)
    monkeypatch.setattr(listen, "reply_to_message", lambda *a: (_ for _ in ()).throw(
        session_effects.Refused("loop", session_effects.LOOP.format(where=WS))))
    assert listen._deliver_pending(paths, "C0", None) is False
    assert len(listen.read_pending(paths)) == 1
    assert "is waiting and will be tried again. Assistant already typed this into workspace:1000010005" in _log(paths)


def test_a_message_that_gave_up_after_hours_is_told_so_once(env_delivery, monkeypatch):
    paths, calls = env_delivery
    rec = _msg(MARKER, age_sec=4 * 3600)
    listen.add_pending(paths, rec)
    serve(monkeypatch)
    assert listen._deliver_pending(paths, "C0", None) is True
    [(text, thread)] = replies(calls)
    assert text.startswith("I couldn't deliver your message (sent ") and thread == rec["msg_ts"]


def test_each_message_in_a_batch_has_its_own_intent_begun_before_any_keystroke(reply, env_delivery):
    cmux, _ = reply
    first = {"channel": "C0", "text": "one", "msg_ts": "1793613610.000100", "author": "UOWNER"}
    second = {"channel": "C0", "text": "two", "msg_ts": "1793613611.000100", "author": "UOWNER"}
    listen.reply_to_message(cl.Paths.from_env(), dict(SESS), [first, second])
    assert [r["kind"] for r in cmux.first] == ["intent", "intent"]
    assert sorted(r["target"]["ts"] for r in cmux.first) == ["1793613610.000100", "1793613611.000100"]
    assert [r["outcome"] for r in rows() if r["kind"] == "result"] == ["done", "done"]


def test_an_older_message_already_typed_drops_only_itself_and_the_rest_wait(reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    old, new = _msg("old", age_sec=30), _msg("new")
    for rec in (old, new):
        listen.add_pending(paths, rec)
    serve(monkeypatch)
    session_effects.finish(session_effects.begin(
        WS, "slack-message", kind="tap", ref="slack:x", component="comms", slack_ts=old["msg_ts"],
        anchor=float(old["msg_ts"])), "unknown")
    assert listen._deliver_pending(paths, "C0", None) is False
    assert [r["text"] for r in listen.read_pending(paths)] == ["new"] and cmux.calls == []
    assert [thread for _text, thread in replies(calls)] == [old["msg_ts"]]


# --- boot, trust, clear -----------------------------------------------------------------------------------------

def test_the_trust_answer_and_boot_prompt_are_recorded_as_the_standing_slack_setting(tmp_path, monkeypatch):
    paths_, closed = _spawn_env(tmp_path, monkeypatch, ws=WS, surface="surface:310")
    cmux = Cmux(monkeypatch)
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
    intents = [r for r in rows() if r["kind"] == "intent"]
    assert [r["target"]["source"] for r in intents] == ["trust", "boot"]
    assert {(r["auth"]["kind"], r["auth"]["ref"]) for r in intents} == {("standing", "slack-comms")}
    assert [m for m, _ in cmux.calls] == ["surface.send_text"] * 4
    assert [r["outcome"] for r in rows() if r["kind"] == "result"] == ["done", "done"]


def test_a_boot_prompt_that_cant_be_recorded_is_not_typed_and_the_spawn_is_abandoned(tmp_path, monkeypatch):
    paths_, closed = _spawn_env(tmp_path, monkeypatch, ws=WS)
    cmux = Cmux(monkeypatch)
    logs: list = []
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    assert cs.spawn_session(paths_, Path("/boot.md"), log=logs.append, agent=ag.CLAUDE) is None
    assert cmux.calls == [] and closed == [WS]
    assert any("boot prompt not typed" in line for line in logs)


def test_a_trust_answer_that_cant_be_recorded_is_not_typed(tmp_path, monkeypatch):
    paths_, closed = _spawn_env(tmp_path, monkeypatch, ws=WS)
    cmux = Cmux(monkeypatch)
    logs: list = []
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    monkeypatch.setattr(cs, "await_ready", lambda **kw: (kw["answer_trust"]() or (False, True)))
    assert cs.spawn_session(paths_, Path("/boot.md"), log=logs.append, agent=ag.CLAUDE) is None
    assert cmux.calls == [] and any("trust answer not typed" in line for line in logs)


def test_clear_and_the_boot_after_it_are_recorded_in_order(paths, monkeypatch):
    _stub_cmux(monkeypatch)
    cmux = Cmux(monkeypatch)
    monkeypatch.setattr(cs, "deliver_boot",
                        lambda p, surface, cwd, prompt, agent: cs._active_keys(surface).text("boot") and "/new.jsonl")
    cs.clear_session(paths, {"ws_ref": WS, "surface_ref": "surface:3", "cwd": "/cwd"}, Path("/boot.md"),
                     agent=ag.CLAUDE)
    intents = [r for r in rows() if r["kind"] == "intent"]
    assert [r["target"]["source"] for r in intents] == ["clear", "boot"]
    assert cmux.first[0]["target"]["source"] == "clear" and len(cmux.first) == 1
    assert cmux.calls[:2] == [("surface.send_text", "/clear"), ("surface.send_text", "\r")]


def test_a_clear_that_cant_be_recorded_is_skipped_and_the_session_kept(paths, monkeypatch):
    _stub_cmux(monkeypatch)
    cmux = Cmux(monkeypatch)
    logs: list = []
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    sess = {"ws_ref": WS, "surface_ref": "surface:3", "cwd": "/cwd"}
    assert cs.clear_session(paths, sess, Path("/boot.md"), agent=ag.CLAUDE, log=logs.append) == sess
    assert cmux.calls == [] and any(f"not clearing {WS}" in line for line in logs)


def test_typing_outside_a_recorded_scope_is_refused_and_a_scope_is_per_thread(paths, monkeypatch):
    cmux = Cmux(monkeypatch)
    with pytest.raises(session_effects.Refused):
        cs.send_enter(paths, "surface:9")
    with pytest.raises(session_effects.Refused):
        cs.submit(paths, "surface:9", "x", "m", lambda: False)
    seen = []
    with cs.typing_scope(paths, WS2, "surface:9", "clear", kind="standing", ref="slack-comms"):
        t = threading.Thread(target=lambda: seen.append(getattr(cs._scope, "keys", None)))
        t.start()
        t.join()
        with pytest.raises(session_effects.Refused):
            cs.send_enter(paths, "surface:OTHER")  # a scope belongs to one terminal
    assert seen == [None] and cmux.calls == []


def test_an_error_inside_a_scope_after_typing_leaves_it_unknown_and_before_typing_failed(paths, monkeypatch):
    Cmux(monkeypatch)
    with pytest.raises(RuntimeError):
        with cs.typing_scope(paths, WS2, "surface:9", "clear", kind="standing", ref="slack-comms") as keys:
            keys.text("/clear")
            raise RuntimeError("boom")
    assert rows()[1]["outcome"] == "unknown"
    with pytest.raises(RuntimeError):
        with cs.typing_scope(paths, WS, "surface:9", "boot", kind="standing", ref="slack-comms"):
            raise RuntimeError("boom")
    assert rows()[3]["outcome"] == "failed"


def test_a_refusal_inside_a_scope_before_anything_was_typed_is_failed_not_unknown(paths, monkeypatch):
    Cmux(monkeypatch)
    with pytest.raises(session_effects.Refused):
        with cs.typing_scope(paths, WS2, "surface:9", "clear", kind="standing", ref="slack-comms"):
            cs.send_enter(paths, "surface:OTHER")
    assert rows()[1]["outcome"] == "failed"


def test_keys_that_cant_be_made_close_the_scopes_intent_as_failed(paths, monkeypatch):
    Cmux(monkeypatch)
    monkeypatch.setattr(session_effects, "Keys", lambda *a, **k: (_ for _ in ()).throw(
        session_effects.Refused("invalid", "x")))
    with pytest.raises(session_effects.Refused):
        with cs.typing_scope(paths, WS2, "surface:9", "clear", kind="standing", ref="slack-comms"):
            pass
    assert rows()[1]["outcome"] == "failed"


def test_a_batch_whose_primary_refuses_closes_the_older_intents_as_failed(paths, monkeypatch):
    Cmux(monkeypatch)
    older = [{"ref": "slack:1793613610.000100", "anchor": 1793613610.0, "slack_ts": "1793613610.000100"}]
    with pytest.raises(session_effects.Refused) as exc:
        with cs.typing_scope(paths, WS2, "surface:9", "slack-message", kind="tap", batch=older,
                             ref="slack:later", anchor=time.time() + 7200, slack_ts="1793613611.000100"):
            pass
    assert exc.value.slack_ts == "1793613611.000100" and exc.value.code == "later"
    assert [r["outcome"] for r in rows() if r["kind"] == "result"] == ["failed"]


# --- only the owner's messages are typed -------------------------------------------------------------------------

def _config(paths, target):
    paths.config.write_text(json.dumps({"slack": {"target": target, "allowed_targets": [target]}}))


@pytest.mark.parametrize("target, owner_env, author, typed", [
    ("U0OWNER", "", "U0OWNER", True),        # a direct message: the target is the owner
    ("U0OWNER", "", "U0OTHER", False),
    ("W0OWNER", "", "W0OWNER", True),        # an Enterprise Grid id counts like a U id
    ("C0CHAN", "U0OWNER", "U0OWNER", True),  # a channel: the owner's id comes from SLACK_USER_ID
    ("C0CHAN", "W0OWNER", "W0OWNER", True),
    ("C0CHAN", "U0OWNER", "U0OTHER", False),
    ("C0CHAN", "U0OWNER", "", False),        # a message with no author is never the owner's
    ("C0CHAN", "", "U0OWNER", True),         # a channel and no owner id: typed, as it was before there was a rule
    ("C0CHAN", "", "U0ANYONE", True),        # and so is anyone's there: there is no owner to compare with
    ("C0CHAN", "", "", True),
    ("G0PRIVATE", "", "U0OWNER", True),      # a private channel is a channel
    ("C0CHAN", "not-an-id", "U0OWNER", True),  # an id of the wrong shape names nobody, so the owner is unknown
    ("D0DM", "", "U0OWNER", True),           # a direct-message conversation holds only the owner
    ("D0DM", "", "", False),
])
def test_only_the_owners_message_is_typed(env_delivery, monkeypatch, target, owner_env, author, typed):
    paths, _ = env_delivery
    _config(paths, target)
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    if owner_env:
        monkeypatch.setenv("SLACK_USER_ID", owner_env)
    assert listen.typed_for(paths, {"msg_ts": TS, "author": author, "channel": "C0"}) is typed
    assert (_log(paths) == "") is typed


def test_the_log_says_in_a_sentence_why_a_message_was_not_typed(env_delivery, monkeypatch):
    paths, _ = env_delivery
    _config(paths, "C0CHAN")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")
    listen.typed_for(paths, {"msg_ts": TS, "author": "U0OTHER", "channel": "C0CHAN"})
    listen.typed_for(paths, {"msg_ts": TS, "author": "", "channel": "C0CHAN"})
    log = _log(paths)
    assert f"Slack message {TS} in C0CHAN was written by U0OTHER, not by you (U0OWNER), so Assistant kept it as " \
           "received and did not type it into the session." in log
    assert f"Slack message {TS} in C0CHAN has no author, so Assistant did not type it into the session." in log
    assert len(log.splitlines()) == 2


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

    def fake_worker(channel_id, wake, stop_, env_):
        wake.get(timeout=5)
        stop_.set()

    def fake_poll(stop_, env_, msg_queue):
        msg_queue.put({**_msg(MARKER), "msg_ts": "1793613611.000100", "author": "U0OTHER"})
        msg_queue.put({**_msg("mine"), "msg_ts": TS, "author": "U0OWNER"})

    class QuickQueue(listen.queue.Queue):
        def get(self, block=True, timeout=None):  # the loop's one-second look at its queue, without the second
            return super().get(block, 0.02 if timeout == 1 else timeout)

    monkeypatch.setattr(listen.queue, "Queue", QuickQueue)
    monkeypatch.setattr(listen, "_channel_worker", fake_worker)
    monkeypatch.setattr(listen, "_poll_thread", fake_poll)
    t = threading.Thread(target=listen.inbound_loop, args=(stop, {}), daemon=True)
    t.start()
    t.join(timeout=10)
    assert [r["text"] for r in listen.read_pending(paths)] == ["mine"]
    appended = [a[a.index("--text") + 1] for a in calls if "conversation.py" in a[0] and "append" in a]
    assert appended == [MARKER, "mine"], "both are recorded as received"
    assert "was written by U0OTHER, not by you (U0OWNER)" in _log(paths)
    assert replies(calls) == [(OTHER,
                               "1793613611.000100")]


def test_messages_queued_before_the_owner_check_are_checked_again_when_they_are_delivered(
        reply, env_delivery, monkeypatch):
    cmux, _ = reply
    paths, calls = env_delivery
    pending(paths, author="U0OTHER")
    serve(monkeypatch)
    assert listen._deliver_pending(paths, "C0", None) is True
    assert cmux.calls == [] and listen.read_pending(paths) == []
    assert [text for text, _ in replies(calls)] == [listen.OTHER_MEMBER_REPLY.format(owner="UOWNER")]


# --- a channel where Assistant can't tell who the owner is: typed as before ------------------------------------

def test_with_a_channel_and_no_owner_id_every_message_is_queued_for_typing_and_nothing_is_said(
        env_delivery, monkeypatch):
    """What broke on 2026-11-25: the owner's own install had a channel as its target and no SLACK_USER_ID, and after
    the update every message was refused with "I can't tell which Slack account is yours"."""
    paths, calls = env_delivery
    _config(paths, "C0")
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    monkeypatch.setattr(listen, "ensure_warm_session", lambda p, **kw: None)
    stop = threading.Event()

    def fake_worker(channel_id, wake, stop_, env_):
        wake.get(timeout=5)
        wake.get(timeout=5)
        stop_.set()

    def fake_poll(stop_, env_, msg_queue):
        msg_queue.put({**_msg("mine"), "msg_ts": TS, "author": "U0OWNER"})
        msg_queue.put({**_msg("a colleague's"), "msg_ts": "1793613611.000100", "author": "U0OTHER"})

    class QuickQueue(listen.queue.Queue):
        def get(self, block=True, timeout=None):  # the loop's one-second look at its queue, without the second
            return super().get(block, 0.02 if timeout == 1 else timeout)

    monkeypatch.setattr(listen.queue, "Queue", QuickQueue)
    monkeypatch.setattr(listen, "_channel_worker", fake_worker)
    monkeypatch.setattr(listen, "_poll_thread", fake_poll)
    t = threading.Thread(target=listen.inbound_loop, args=(stop, {}), daemon=True)
    t.start()
    t.join(timeout=10)
    assert stop.is_set(), "both messages reached the worker"
    assert [r["text"] for r in listen.read_pending(paths)] == ["mine", "a colleague's"]
    assert replies(calls) == [] and "did not type" not in _log(paths)
    assert list(paths.comms_dir.glob("ignored-*")) == []


@pytest.mark.parametrize("author", ["U0ANYONE", None])  # None: queued by a version that didn't keep the author
def test_a_message_queued_in_a_channel_with_no_owner_id_is_typed_as_before(reply, env_delivery, monkeypatch, author):
    cmux, _ = reply
    paths, calls = env_delivery
    _config(paths, "C0CHAN")
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    rec = {**_msg(MARKER, channel="C0CHAN"), "author": author}
    listen.add_pending(paths, {k: v for k, v in rec.items() if v is not None or k != "author"})
    serve(monkeypatch)
    assert listen._deliver_pending(paths, "C0CHAN", None) is True
    assert any(MARKER in str(text) for text in cmux.typed()) and listen.read_pending(paths) == []
    assert replies(calls) == []


# --- telling someone else their message wasn't acted on --------------------------------------------------------

def _ignored(author="U0OWNER", ts=TS, channel="C0CHAN"):
    return {"channel": channel, "msg_ts": ts, "author": author, "text": MARKER}


def test_someone_elses_message_gets_one_reply_a_day_for_each_author(env_delivery, monkeypatch):
    paths, calls = env_delivery
    _config(paths, "C0CHAN")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")
    listen.tell_ignored(paths, _ignored("U0OTHER"), None)
    listen.tell_ignored(paths, _ignored("U0OTHER", ts="1793613999.000100"), None)
    listen.tell_ignored(paths, _ignored("U0THIRD", ts="1793614000.000100"), None)
    assert replies(calls) == [(OTHER, TS),                    # each in the thread of its own message
                              (OTHER,
                               "1793614000.000100")]
    marker = paths.comms_dir / "ignored-author-C0CHAN-U0OTHER.json"
    marker.write_text(json.dumps({"ts": time.time() - 25 * 3600}))
    listen.tell_ignored(paths, _ignored("U0OTHER", ts="1793614001.000100"), None)
    assert len(replies(calls)) == 3  # once the marker is 25 hours old, one again


@pytest.mark.parametrize("target, owner, author", [
    ("C0CHAN", "U0OWNER", "U0OWNER"),   # the owner's own message
    ("D0DM", "", "U0OWNER"),            # a direct-message conversation
    ("U0OWNER", "", "U0OWNER"),         # a person as the target
    ("C0CHAN", "U0OWNER", ""),          # nobody wrote it
    ("C0CHAN", "", "U0OWNER"),          # a channel and no owner id: it was typed
    ("C0CHAN", "", ""),
])
def test_no_reply_goes_for_a_message_that_was_typed_or_has_no_author(env_delivery, monkeypatch, target, owner, author):
    paths, calls = env_delivery
    _config(paths, target)
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    if owner:
        monkeypatch.setenv("SLACK_USER_ID", owner)
    listen.tell_ignored(paths, _ignored(author), None)
    assert replies(calls) == []


def test_a_marker_that_cant_be_written_means_no_reply(env_delivery, monkeypatch):
    paths, calls = env_delivery
    _config(paths, "C0CHAN")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    listen.tell_ignored(paths, _ignored("U0OTHER"), None)
    assert replies(calls) == [] and "did not send its reply about the ignored message" in _log(paths)


def test_a_reply_that_fails_to_send_is_logged(env_delivery, monkeypatch):
    paths, calls = env_delivery
    _config(paths, "C0CHAN")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")
    monkeypatch.setattr(listen, "cli", lambda argv, timeout=30, env=None: (1, "", "slack is down"))
    listen.tell_ignored(paths, _ignored("U0OTHER"), None)
    assert "did not send (rc=1): slack is down" in _log(paths)


def test_a_dropped_note_that_fails_to_send_is_logged(env_delivery, monkeypatch):
    paths, calls = env_delivery
    monkeypatch.setattr(listen, "cli", lambda argv, timeout=30, env=None: (1, "", "slack is down"))
    listen._tell_dropped(paths, {"channel": "C0", "msg_ts": TS}, None, listen.DROPPED_EXPIRED)
    assert "did not send (rc=1): slack is down" in _log(paths)


def test_a_message_time_that_isnt_a_time_still_gets_a_clock_for_the_note():
    assert listen._clock("not a time") == "earlier" and listen._clock(None) == "earlier"


def test_the_doctor_warns_when_a_channel_is_the_target_and_the_owner_is_unknown(env_delivery, monkeypatch):
    from test_doctor import _load as load_doctor
    doctor = load_doctor()
    paths, _ = env_delivery
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    _config(paths, "C0CHAN")
    check = doctor.check_slack_owner()
    assert check.status == doctor.WARN and "launchctl kickstart -k gui/$UID/com.assistant.assistant-comms" in check.remedy
    assert doctor.check_slack_owner in doctor.SLACK_CHECKS
    assert "not ~/.zshrc" in check.remedy  # the service reads ~/.zprofile, so that is where the id must be set
    assert "types every member's messages into the session, as it always has" in check.detail
    assert "types none" not in check.detail and check.remedy.startswith("to have only your own messages typed, add ")
    paths.config.write_text(json.dumps({"slack": {"target": "C0CHAN", "owner": "U0OWNER", "allowed_targets": ["C0CHAN"]}}))
    assert doctor.check_slack_owner().status == doctor.PASS  # slack.owner names the owner, as it does for the reactor
    _config(paths, "C0CHAN")
    monkeypatch.setenv("SLACK_USER_ID", "U0OWNER")
    assert doctor.check_slack_owner().status == doctor.PASS
    monkeypatch.delenv("SLACK_USER_ID")
    _config(paths, "U0OWNER")
    assert doctor.check_slack_owner().status == doctor.PASS
    paths.config.unlink()
    assert doctor.check_slack_owner().status == doctor.SKIP


def test_a_message_sitting_in_the_prompt_box_is_told_from_one_that_isnt(monkeypatch, paths):
    box = lambda text: "─" * 20 + f"\n❯ {text}\n" + "─" * 20
    monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=200: box("[slack msg_ts=5] hi"))
    assert cs.message_in_box(paths, "surface:3", "msg_ts=5", ag.CLAUDE) is True
    assert cs.message_in_box(paths, "surface:3", "msg_ts=6", ag.CLAUDE) is False
    monkeypatch.setattr(cs, "_surface_read_text", lambda p, ref, lines=200: box(""))
    assert cs.message_in_box(paths, "surface:3", "msg_ts=5") is False


def test_run_cmd_remembers_how_the_last_command_ended_on_this_thread(monkeypatch):
    import subprocess as sp
    cl.clear_last_run()
    assert cl.last_run() is None
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, "out", ""))
    assert cl.run_cmd(["x"]) == (0, "out", "") and cl.last_run() == (0, "")
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 3, "", "bad"))
    cl.run_cmd(["x"])
    assert cl.last_run() == (3, "bad")

    def slow(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=5)
    monkeypatch.setattr(sp, "run", slow)
    assert cl.run_cmd(["x"], timeout=5) == (-1, "", "timeout after 5s") and cl.last_run() == (-1, "timeout after 5s")

    def missing(*a, **k):
        raise FileNotFoundError("no cmux")
    monkeypatch.setattr(sp, "run", missing)
    assert cl.run_cmd(["x"])[0] == -1 and cl.last_run() == (-1, "no cmux")
    seen = []
    t = threading.Thread(target=lambda: seen.append(cl.last_run()))
    t.start()
    t.join()
    assert seen == [None]  # another thread never sees this thread's last command
    cl.clear_last_run()
    assert cl.last_run() is None


def test_the_typing_transport_tells_a_timeout_a_refusal_and_a_silent_success_apart(paths, monkeypatch):
    """Through the real run_cmd, with only the process faked."""
    import subprocess as sp
    outcomes = iter([sp.TimeoutExpired(cmd="cmux", timeout=15), sp.CompletedProcess([], 1, "", "no"),
                     sp.CompletedProcess([], 0, "", ""), sp.CompletedProcess([], 0, "{\"ok\": true}", "")])

    def fake(*a, **k):
        out = next(outcomes)
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(sp, "run", fake)
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        assert keys.text("a") is False and keys.raised           # a timeout: may have landed
        keys.raised = False
        assert keys.text("b") is False                           # refused
        assert keys.text("c") is True and keys.text("d") is True  # exit status 0, whatever it printed


# --- a person as the target: notices go where slack-send.py's gate allows -----------------------------------------

def _real_gate(monkeypatch, paths, target="U0OWNER"):
    """The fake cli, except that every slack-send.py call goes through the real send-gate (in dry mode, so no network):
    a notice to a conversation the gate would refuse comes back with exit status 1."""
    import contextlib
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location("slack_send_real_gate", Path(listen.__file__).parent / "slack-send.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    paths.config.write_text(json.dumps({"slack": {"target": target, "allowed_targets": [target]}}))
    sent = []

    def cli(argv, timeout=30, env=None):
        if "slack-send.py" not in argv[0]:
            return 0, "", ""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gate.main(argv[1:] + ["--dry-run"], paths=paths, env={})
        sent.append((argv[argv.index("--channel") + 1], rc, out.getvalue() + err.getvalue()))
        return rc, out.getvalue(), err.getvalue()

    monkeypatch.setattr(listen, "cli", cli)
    return sent


DM = "D0DIRECT01"


def test_with_a_person_as_the_target_every_notice_goes_through_the_real_send_gate(env_delivery, monkeypatch):
    paths, _ = env_delivery
    sent = _real_gate(monkeypatch, paths)
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    listen._tell_dropped(paths, {"channel": DM, "msg_ts": TS}, None, listen.DROPPED_UNCONFIRMED)
    listen._notify_restart_once(paths, DM, None)
    listen.tell_ignored(paths, {"channel": DM, "msg_ts": TS, "author": "U0OTHER"}, None)
    assert [(channel, rc) for channel, rc, _ in sent] == [("U0OWNER", 0)] * 3  # the gate let every one through
    assert "refused" not in _log(paths) and "did not send" not in _log(paths)


def test_without_the_fix_the_real_gate_would_refuse_a_notice_for_a_direct_message(env_delivery, monkeypatch):
    paths, _ = env_delivery
    sent = _real_gate(monkeypatch, paths)
    listen.cli(listen._send_args("x", "reply", DM, None), timeout=5)  # what the notices used to do
    assert sent[0][1] == 1 and "send-gate" in sent[0][2]


@pytest.mark.parametrize("target, conversation, goes_to", [
    ("U0OWNER", DM, "U0OWNER"), ("W0OWNER", DM, "W0OWNER"),   # a person as the target
    ("U0OWNER", "C0CHAN", "C0CHAN"),                          # another conversation stays as it is
    ("C0CHAN", "C0CHAN", "C0CHAN"), ("D0DIRECT01", DM, DM)])  # a channel or a D target: already allowed
def test_a_notice_goes_to_the_target_only_for_a_direct_message_with_a_person_as_the_target(
        env_delivery, target, conversation, goes_to):
    paths, _ = env_delivery
    _config(paths, target)
    assert listen.notice_channel(paths, conversation) == goes_to


def test_the_owner_is_worked_out_in_the_order_the_reactor_uses(env_delivery, monkeypatch):
    paths, _ = env_delivery
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    paths.config.write_text(json.dumps({"slack": {"target": "U0TARGET", "owner": "U0CONFIG", "allowed_targets": []}}))
    assert listen.owner_id(paths) == "U0CONFIG"                      # slack.owner beats the target
    assert listen.owner_id(paths, {"SLACK_USER_ID": "U0ENV"}) == "U0ENV"  # and the environment beats both
    paths.config.write_text(json.dumps({"slack": {"target": "U0TARGET", "owner": "not an id"}}))
    assert listen.owner_id(paths) == "U0TARGET"
    paths.config.write_text("{ not json")
    assert listen.owner_id(paths) == ""


def test_the_sentences_a_person_can_read_from_the_typing_module():
    from assistant import session_effects as se
    assert se.UNRECORDED.endswith("can be written to.")
    assert se.UNAUTHORIZED == "Assistant couldn't confirm what allowed this, so it typed nothing. Run it again from the place you meant."
    assert se.INVALID == ("Assistant didn't recognise the session this was meant for (it expects workspace:N), so it "
                          "typed nothing.")
    assert se.LATER.endswith("If this Mac's clock is wrong, fix it.")
    assert se.UNKNOWN.endswith("Look at that session. If it isn't there, type it into the session yourself.")
    assert "bin/cmux-send.py" not in se.UNKNOWN_SHORT


def test_a_log_line_about_a_message_leaves_out_the_command_to_run(reply, env_delivery, monkeypatch):
    cmux, state = reply
    paths, calls = env_delivery
    pending(paths)
    state["landed"] = False
    serve(monkeypatch)
    listen._deliver_pending(paths, "C0", None)
    listen._deliver_pending(paths, "C0", None)
    assert "your message was dropped" in _log(paths) and "bin/cmux-send.py" not in _log(paths)
    assert "restart assistant-comms" not in _log(paths)


def test_the_doctor_copes_with_a_config_whose_slack_part_isnt_a_table(env_delivery, monkeypatch):
    from test_doctor import _load as load_doctor
    doctor = load_doctor()
    paths, _ = env_delivery
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    monkeypatch.setenv("SLACK_PING_TARGET", "C0CHAN")  # the target comes from the environment
    paths.config.write_text(json.dumps({"slack": ["not", "a", "table"]}))
    assert doctor.check_slack_owner().status == doctor.WARN


def test_the_doctor_says_both_places_the_owner_can_be_set(env_delivery, monkeypatch):
    from test_doctor import _load as load_doctor
    doctor = load_doctor()
    paths, _ = env_delivery
    monkeypatch.delenv("SLACK_USER_ID", raising=False)
    _config(paths, "C0CHAN")
    assert "neither SLACK_USER_ID nor slack.owner in ~/.assistant/config.json" in doctor.check_slack_owner().detail


@pytest.fixture(autouse=True)
def pro_mode(monkeypatch):
    """Everything this file tests is Pro mode's; the test says so, instead of leaning on the suite's default."""
    from assistant import mode
    monkeypatch.setattr(mode, "current", lambda: "pro")
