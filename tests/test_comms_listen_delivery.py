"""Tests for comms-listen's inbound queue and its three noisy loops.

2026-09-27/28: inbound Slack messages that arrived while no warm session was up
were dropped for good, the heartbeat paged every 30 minutes for 13 days, one
ledger sweep posted 129 updates in two minutes, and one workspace was pinged 21
times in six hours. Every cmux / Slack touchpoint is stubbed; state lives under
tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import queue
import sys
import threading
import time
from pathlib import Path

import comms_lib as cl
import pytest
from assistant.subsystems import comms as subsystem_comms


def _load():
    if "comms_listen" in sys.modules:
        return sys.modules["comms_listen"]
    spec = importlib.util.spec_from_file_location(
        "comms_listen", str(Path(__file__).resolve().parent.parent / "bin" / "comms-listen.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comms_listen"] = mod
    spec.loader.exec_module(mod)
    return mod


listen = _load()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Isolated comms state plus a recording fake for every CLI call."""
    monkeypatch.setattr(listen, "_spawn_failures", 0)
    home = tmp_path / "home"
    (home / ".assistant" / "comms").mkdir(parents=True)
    (home / ".assistant" / "inbox").mkdir(parents=True)
    (home / ".assistant" / "config.json").write_text(json.dumps(
        {"slack": {"target": "C0", "allowed_targets": ["C0"]}}))
    monkeypatch.setenv("COMMS_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    monkeypatch.setattr(listen, "INBOX_DIR", home / ".assistant" / "inbox")
    calls: list[list[str]] = []

    def fake_cli(argv, timeout=30, env=None):
        calls.append(argv)
        if "slack-send.py" in argv[0]:
            return 0, json.dumps({"channel": "C0", "message_id": "9.9"}), ""
        return 0, "", ""

    monkeypatch.setattr(listen, "cli", fake_cli)
    return cl.Paths.from_env(), calls


def _sends(calls):
    return [a[a.index("--text") + 1] for a in calls if "slack-send.py" in a[0]]


def _log(paths):
    p = paths.comms_dir / "comms-listen.log"
    return p.read_text() if p.exists() else ""


def _msg(text, age_sec=0.0, channel="C0"):
    return {"channel": channel, "text": text, "msg_ts": f"{time.time() - age_sec:.6f}",
            "reply_to": None}


# ─── pending queue ──────────────────────────────────────────────────────────


def test_pending_roundtrip_dedups_and_removes(env):
    paths, _ = env
    a, b = _msg("one"), _msg("two")
    listen.add_pending(paths, a)
    listen.add_pending(paths, a)
    listen.add_pending(paths, b)
    assert [r["text"] for r in listen.read_pending(paths)] == ["one", "two"]
    listen.remove_pending(paths, [a["msg_ts"]])
    assert [r["text"] for r in listen.read_pending(paths)] == ["two"]


def test_read_pending_tolerates_bad_files(env):
    paths, _ = env
    p = paths.comms_dir / "pending-inbound.json"
    p.write_text("{not json")
    assert listen.read_pending(paths) == []
    p.write_text(json.dumps({"not": "a list"}))
    assert listen.read_pending(paths) == []
    p.write_text(json.dumps([{"msg_ts": "1"}, "junk"]))
    assert listen.read_pending(paths) == [{"msg_ts": "1"}]


def test_split_pending_filters_channel_orders_oldest_first_and_expires():
    now = 10_000.0
    recs = [{"channel": "C0", "msg_ts": "9990"}, {"channel": "C0", "msg_ts": "9000"},
            {"channel": "C1", "msg_ts": "9995"}, {"channel": "C0", "msg_ts": "1000"}]
    fresh, expired = listen.split_pending(recs, "C0", now, max_age=5000)
    assert [r["msg_ts"] for r in fresh] == ["9000", "9990"]
    assert [r["msg_ts"] for r in expired] == ["1000"]
    fresh, _ = listen.split_pending([{"msg_ts": "9999"}], "default", now, max_age=5000)
    assert len(fresh) == 1, "a message without a channel belongs to 'default'"


def test_message_age_sec_bad_ts_is_zero():
    assert listen.message_age_sec({"msg_ts": "nope"}, 100.0) == 0.0
    assert listen.message_age_sec({}, 100.0) == 0.0
    assert listen.message_age_sec({"msg_ts": "40"}, 100.0) == 60.0


def test_record_inbound_appends_the_turn(env):
    paths, calls = env
    listen._record_inbound({"channel": "C0", "text": "hi", "msg_ts": "1.5", "reply_to": "0.9"})
    [argv] = calls
    assert "conversation.py" in argv[0] and "append" in argv
    assert argv[argv.index("--direction") + 1] == "in"
    assert argv[argv.index("--msg-ts") + 1] == "1.5"
    assert argv[argv.index("--reply-to") + 1] == "0.9"
    listen._record_inbound({"channel": "C0", "text": "hi"})
    assert "--msg-ts" not in calls[-1] and "--reply-to" not in calls[-1]


# ─── _deliver_pending ───────────────────────────────────────────────────────


def _stub_session(monkeypatch, states, replies):
    """_warm_session returns successive (sess, how) states; reply_to_message
    returns successive delivered flags. Records every call."""
    seen = {"warm": [], "replied": []}
    sess = {"ws_ref": "workspace:5", "surface_ref": "surface:5", "cwd": "/cwd"}
    state_iter = iter(states)
    reply_iter = iter(replies)

    def fake_warm(paths, **kw):
        seen["warm"].append(kw)
        how = next(state_iter, listen.SESSION_ALIVE)
        return (None if how == listen.SESSION_NONE else sess), how

    def fake_reply(paths, s, recs):
        seen["replied"].append([r["text"] for r in recs])
        return next(reply_iter), s

    monkeypatch.setattr(listen, "_warm_session", fake_warm)
    monkeypatch.setattr(listen, "reply_to_message", fake_reply)
    return seen


def test_undelivered_message_stays_queued_until_the_session_takes_it(env, monkeypatch):
    """The 2026-09-27 drop: "no warm session — skipping" lost the message. Now a
    failed delivery keeps it queued, replaces the live session that didn't take
    it, and the next pass delivers it. Mutation probe: remove the message from
    the queue before the reply is confirmed and the second pass has nothing to
    deliver."""
    paths, _ = env
    listen.add_pending(paths, _msg("Are you alive?"))
    seen = _stub_session(monkeypatch, [listen.SESSION_ALIVE, listen.SESSION_SPAWNED,
                                       listen.SESSION_ALIVE], [False, True])
    assert listen._deliver_pending(paths, "C0", None) is False
    assert [r["text"] for r in listen.read_pending(paths)] == ["Are you alive?"]
    assert {"replace_ws": "workspace:5"} in seen["warm"], "a session that didn't take input is replaced"
    assert listen._deliver_pending(paths, "C0", None) is True
    assert listen.read_pending(paths) == []
    assert seen["replied"] == [["Are you alive?"], ["Are you alive?"]]


def test_no_session_sends_one_notice_per_outage_and_clears_it_on_delivery(env, monkeypatch):
    paths, calls = env
    listen.add_pending(paths, _msg("Status?", age_sec=120))
    listen.add_pending(paths, _msg("Pulse active now?", age_sec=90))
    _stub_session(monkeypatch, [listen.SESSION_NONE, listen.SESSION_NONE, listen.SESSION_ALIVE],
                  [True])
    assert listen._deliver_pending(paths, "C0", None) is False
    assert listen._deliver_pending(paths, "C0", None) is False
    assert _sends(calls) == [listen.RESTART_NOTICE], "one notice, not one per retry"
    assert listen._restart_notice_path(paths, "C0").exists()
    assert listen._deliver_pending(paths, "C0", None) is True
    assert not listen._restart_notice_path(paths, "C0").exists()
    assert listen.read_pending(paths) == []


def test_a_just_spawned_session_is_not_replaced_right_away(env, monkeypatch):
    """A fresh session that refuses input is left for the next backed-off try
    instead of being swapped for another spawn in the same pass."""
    paths, _ = env
    listen.add_pending(paths, _msg("hi"))
    seen = _stub_session(monkeypatch, [listen.SESSION_SPAWNED], [False])
    assert listen._deliver_pending(paths, "C0", None) is False
    assert seen["warm"] == [{"respawn_on_stale": True}]


def test_notice_marker_cleared_when_everything_expired(env, monkeypatch):
    paths, _ = env
    listen._restart_notice_path(paths, "C0").write_text("{}")
    listen.add_pending(paths, _msg("ancient", age_sec=listen.PENDING_MAX_AGE_SEC + 60))
    _stub_session(monkeypatch, [], [])
    assert listen._deliver_pending(paths, "C0", None) is True
    assert not listen._restart_notice_path(paths, "C0").exists(), "the next outage gets a notice"


def test_restart_notice_is_per_channel():
    paths = cl.Paths.from_env({"HOME": "/h", "COMMS_HOME": "/h"})
    assert listen._restart_notice_path(paths, "C0") != listen._restart_notice_path(paths, "C1")
    assert listen._restart_notice_path(paths, "a/b").name == "restart-notice-a-b.json"


def test_no_notice_for_a_brief_blip(env, monkeypatch):
    paths, calls = env
    listen.add_pending(paths, _msg("hi", age_sec=5))
    _stub_session(monkeypatch, [listen.SESSION_NONE], [])
    assert listen._deliver_pending(paths, "C0", None) is False
    assert _sends(calls) == []


def test_unreachable_cmux_is_not_typed_into_or_replaced(env, monkeypatch):
    paths, _ = env
    listen.add_pending(paths, _msg("hi"))
    seen = _stub_session(monkeypatch, [listen.SESSION_UNREACHABLE], [])
    assert listen._deliver_pending(paths, "C0", None) is False
    assert seen["replied"] == [] and len(seen["warm"]) == 1


def test_messages_too_old_to_answer_are_dropped_with_a_log(env, monkeypatch):
    paths, _ = env
    listen.add_pending(paths, _msg("ancient", age_sec=listen.PENDING_MAX_AGE_SEC + 60))
    seen = _stub_session(monkeypatch, [], [])
    assert listen._deliver_pending(paths, "C0", None) is True
    assert listen.read_pending(paths) == [] and seen["warm"] == []
    assert "giving up on it" in _log(paths)


def test_notice_send_failure_is_logged_and_not_retried(env, monkeypatch):
    paths, _ = env
    monkeypatch.setattr(listen, "cli", lambda argv, timeout=30, env=None: (1, "", "boom"))
    listen._notify_restart_once(paths, "C0", None)
    listen._notify_restart_once(paths, "C0", None)
    assert _log(paths).count("restart notice rc=1") == 1


# ─── _channel_worker ────────────────────────────────────────────────────────


class ScriptedQueue:
    """get() hands out scripted items, then trips `stop` when they run out."""

    def __init__(self, items, stop):
        self.items = list(items)
        self.stop = stop

    def get(self, timeout=None):
        if not self.items:
            self.stop.set()
            raise queue.Empty
        item = self.items.pop(0)
        if item is None:
            raise queue.Empty
        return item


def test_channel_worker_delivers_on_wake_and_backs_off_retries(env, monkeypatch):
    """A wake-up delivers at once; after a failure the next try waits
    pending_retry_delay, timed from the END of the failed try."""
    paths, _ = env
    clock = {"now": 1000.0}
    monkeypatch.setattr(listen.time, "time", lambda: clock["now"])
    attempts: list[float] = []
    results = iter([False, True])

    def fake_deliver(p, channel, e):
        attempts.append(clock["now"])
        clock["now"] += 120  # a try that waited on a spawn
        return next(results)

    monkeypatch.setattr(listen, "_deliver_pending", fake_deliver)
    monkeypatch.setattr(listen, "PENDING_RETRY_SEC", 30)
    stop = threading.Event()

    class Ticks(ScriptedQueue):
        def get(self, timeout=None):
            clock["now"] += 10
            return super().get(timeout)

    listen._channel_worker("C0", Ticks([True, None, None, None, None], stop), stop)
    # The failed try ran 1010→1130; the retry waits 30s from 1130, not from 1010.
    assert attempts == [1010.0, 1160.0], "retry 30s after the failed try ended, not at once"
    assert "inbound: retrying in 30s" in _log(paths)


def test_pending_retry_delay_doubles_and_caps():
    assert listen.pending_retry_delay(1) == listen.PENDING_RETRY_SEC
    assert listen.pending_retry_delay(2) == listen.PENDING_RETRY_SEC * 2
    assert listen.pending_retry_delay(50) == listen.PENDING_RETRY_MAX_SEC


def test_channel_worker_picks_up_messages_queued_before_a_restart(env, monkeypatch):
    paths, _ = env
    listen.add_pending(paths, _msg("from before the restart"))
    attempts: list[str] = []
    monkeypatch.setattr(listen, "_deliver_pending",
                        lambda p, channel, e: attempts.append(channel) or True)
    stop = threading.Event()
    listen._channel_worker("C0", ScriptedQueue([None], stop), stop)
    assert attempts == ["C0"]


def test_channel_worker_survives_a_delivery_error(env, monkeypatch):
    paths, _ = env

    def boom(p, channel, e):
        raise OSError("disk full")

    monkeypatch.setattr(listen, "_deliver_pending", boom)
    stop = threading.Event()
    listen._channel_worker("C0", ScriptedQueue([True], stop), stop)
    assert "inbound delivery error (will retry): OSError: disk full" in _log(paths)


def test_inbound_loop_records_and_queues_before_the_worker_sees_it(env, monkeypatch):
    """The slack cursor has moved past a message by the time it's polled, so it
    must be on disk before a (possibly busy) worker gets it."""
    paths, calls = env
    listen.add_pending(paths, _msg("queued", channel="C1"))
    monkeypatch.setattr(listen, "ensure_warm_session", lambda p, **kw: None)
    started: list[str] = []
    seen_on_disk: list[list[str]] = []
    stop = threading.Event()

    def fake_worker(channel_id, wake, stop_, env_):
        started.append(channel_id)
        if channel_id == "C0":
            wake.get(timeout=5)
            wake.get(timeout=5)
            seen_on_disk.append(sorted(r["text"] for r in listen.read_pending(paths)))
            stop_.set()

    def fake_poll(stop_, env_, msg_queue):
        msg_queue.put(_msg("new"))
        msg_queue.put(_msg("newer"))

    monkeypatch.setattr(listen, "_channel_worker", fake_worker)
    monkeypatch.setattr(listen, "_poll_thread", fake_poll)
    t = threading.Thread(target=listen.inbound_loop, args=(stop, {}), daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive()
    assert sorted(started) == ["C0", "C1"], "one worker per channel, reused for later messages"
    assert seen_on_disk == [["new", "newer", "queued"]]
    assert any("conversation.py" in a[0] and "append" in a for a in calls)


# ─── heartbeat ──────────────────────────────────────────────────────────────


def test_heartbeat_action_pages_once_after_confirmation_and_recovers_once():
    assert listen.heartbeat_action(1, paged=False, confirm=2) is None
    assert listen.heartbeat_action(2, paged=False, confirm=2) == "page"
    assert listen.heartbeat_action(9, paged=True, confirm=2) is None
    assert listen.heartbeat_action(0, paged=True, confirm=2) == "recover"
    assert listen.heartbeat_action(0, paged=False, confirm=2) is None


def test_heartbeat_loop_sends_one_page_and_one_recovery(env, monkeypatch):
    """621 identical pages went out during the 13-day pulse outage. Now: one
    page after two stale checks, silence while it stays stale, one recovery."""
    paths, calls = env
    stale = int(time.time()) - 5000
    script = [stale, stale, stale, stale, int(time.time())]
    paths.heartbeat.write_text(json.dumps({"last_pulse_ts": script[0]}))
    monkeypatch.setattr(cl, "fmt_heartbeat_alert", lambda hb, age: f"PAGE {age // 1000}k")
    monkeypatch.setattr(cl, "fmt_heartbeat_recovered", lambda hb, down: f"BACK {down}", raising=False)
    stop = threading.Event()
    ticks = {"n": 0}

    def fake_wait(timeout=None):
        ticks["n"] += 1
        if ticks["n"] >= len(script):
            stop.set()
            return True
        paths.heartbeat.write_text(json.dumps({"last_pulse_ts": script[ticks["n"]]}))
        return False

    sent_after_tick: list[int] = []

    def fake_wait_recording(timeout=None):
        sent_after_tick.append(len(_sends(calls)))
        return fake_wait(timeout)

    monkeypatch.setattr(stop, "wait", fake_wait_recording)
    listen.heartbeat_loop(stop, {})
    sent = _sends(calls)
    assert sent_after_tick[0] == 0, "no page after a single stale check"
    assert sent_after_tick[1] == 1, "the page goes out on the second stale check"
    assert sent[0].startswith("PAGE") and len(sent) == 2
    assert sent[1] == f"BACK {script[-1] - stale}"
    kinds = [a[a.index("--kind") + 1] for a in calls if "slack-send.py" in a[0]]
    assert kinds == ["urgent", "action"]


def test_heartbeat_page_that_failed_is_retried_later_not_every_check(env, monkeypatch):
    """A page that fails stays unsent, so it's tried again — but only once per
    HEARTBEAT_PAGE_RETRY_SEC, not on every 60s check."""
    paths, _ = env
    paths.heartbeat.write_text(json.dumps({"last_pulse_ts": int(time.time()) - 5000}))
    monkeypatch.setattr(cl, "fmt_heartbeat_alert", lambda hb, age: "PAGE")
    tries: list[float] = []
    clock = {"now": time.time()}

    def failing_cli(argv, timeout=30, env=None):
        if "slack-send.py" in argv[0]:
            tries.append(clock["now"])
            return 1, "", "slack down"
        return 0, "", ""

    monkeypatch.setattr(listen, "cli", failing_cli)
    monkeypatch.setattr(listen.time, "time", lambda: clock["now"])
    stop = threading.Event()
    steps = [60] * 8 + [listen.HEARTBEAT_PAGE_RETRY_SEC]
    ticks = {"n": 0}

    def fake_wait(timeout=None):
        if ticks["n"] >= len(steps):
            stop.set()
            return True
        clock["now"] += steps[ticks["n"]]
        ticks["n"] += 1
        return False

    monkeypatch.setattr(stop, "wait", fake_wait)
    listen.heartbeat_loop(stop, {})
    assert len(tries) == 2, "one try, then silence for the retry window, then one more"
    assert tries[1] - tries[0] >= listen.HEARTBEAT_PAGE_RETRY_SEC


def test_a_new_outage_right_after_recovery_pages_at_once(env, monkeypatch):
    """The failed-page retry wait must not delay the page for the next outage."""
    paths, calls = env
    now = time.time()
    script = [now - 5000, now - 5000, now, now - 5000, now - 5000]
    paths.heartbeat.write_text(json.dumps({"last_pulse_ts": int(script[0])}))
    monkeypatch.setattr(cl, "fmt_heartbeat_alert", lambda hb, age: "PAGE")
    monkeypatch.setattr(cl, "fmt_heartbeat_recovered", lambda hb, down: "BACK")
    stop = threading.Event()
    ticks = {"n": 0}

    def fake_wait(timeout=None):
        ticks["n"] += 1
        if ticks["n"] >= len(script):
            stop.set()
            return True
        paths.heartbeat.write_text(json.dumps({"last_pulse_ts": int(script[ticks["n"]])}))
        return False

    monkeypatch.setattr(stop, "wait", fake_wait)
    listen.heartbeat_loop(stop, {})
    assert _sends(calls) == ["PAGE", "BACK", "PAGE"]


def test_heartbeat_loop_bad_status_pages_and_missing_config_is_quiet(env, monkeypatch):
    paths, calls = env
    paths.heartbeat.write_text(json.dumps({"last_pulse_ts": int(time.time()), "status": "frozen"}))
    monkeypatch.setattr(cl, "fmt_heartbeat_alert", lambda hb, age: "PAGE")
    stop = threading.Event()
    ticks = {"n": 0}

    def fake_wait(timeout=None):
        ticks["n"] += 1
        if ticks["n"] == 2:
            paths.config.unlink()
            paths.heartbeat.write_text("{broken")
        if ticks["n"] >= 4:
            stop.set()
            return True
        return False

    monkeypatch.setattr(stop, "wait", fake_wait)
    listen.heartbeat_loop(stop, {})
    assert _sends(calls) == ["PAGE"]


# ─── ledger ─────────────────────────────────────────────────────────────────


def test_housekeeping_kinds_are_suppressed():
    for kind in ("decision-transition", "strategist-autopause", "stranded", "skipped"):
        assert listen._suppress_reason({"kind": kind, "key": "k", "outcome": "failed"})
    assert listen._suppress_reason({"kind": "cleanup", "key": "k", "outcome": "verified"}) is None


def test_subsystem_mirrors_housekeeping_kinds():
    assert subsystem_comms.HOUSEKEEPING_KINDS == listen.HOUSEKEEPING_KINDS


def test_plan_broadcast_caps_the_pass():
    entries = ([{"kind": "decision-transition", "key": f"d{i}"} for i in range(3)]
               + [{"kind": "cleanup", "key": f"c{i}"} for i in range(8)])
    to_send, suppressed, overflow = listen.plan_broadcast(entries, max_send=5)
    assert [e["key"] for e in to_send] == ["c0", "c1", "c2", "c3", "c4"]
    assert len(suppressed) == 3 and overflow == 3
    assert listen.plan_broadcast([], max_send=5) == ([], [], 0)


def test_fmt_overflow_plural():
    assert listen.fmt_overflow(1).startswith("…and 1 more Assistant update.")
    assert listen.fmt_overflow(4).startswith("…and 4 more Assistant updates.")


def _ledger(paths, entries):
    paths.ledger.write_text("".join(json.dumps(e) + "\n" for e in entries))
    paths.cursor.write_text("0")


def _one_pass(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda timeout=None: stop.set() or True)
    return stop


def test_ledger_loop_posts_the_cap_then_one_summary(env, monkeypatch):
    paths, calls = env
    _ledger(paths, [{"kind": "cleanup", "key": f"c{i}", "outcome": "verified"} for i in range(7)]
            + [{"kind": "decision-transition", "key": "d1"}])
    monkeypatch.setattr(listen, "LEDGER_MAX_PER_PASS", 5)
    monkeypatch.setattr(cl, "fmt_action_line", lambda e: f"ACTION {e['key']}")
    listen.ledger_loop(_one_pass(monkeypatch), {})
    sent = _sends(calls)
    assert sent[:5] == [f"ACTION c{i}" for i in range(5)]
    assert sent[5] == listen.fmt_overflow(2) and len(sent) == 6
    mirrored = [a for a in calls if "conversation.py" in a[0]]
    assert len(mirrored) == 6, "each post, summary included, is mirrored as an out turn"
    assert "suppressed broadcast key=d1" in _log(paths)


def test_ledger_loop_without_target_sends_nothing(env, monkeypatch):
    paths, calls = env
    paths.config.write_text(json.dumps({"slack": {"target": "", "allowed_targets": []}}))
    _ledger(paths, [{"kind": "cleanup", "key": "c1", "outcome": "verified"}])
    listen.ledger_loop(_one_pass(monkeypatch), {})
    assert _sends(calls) == []
    assert "no target configured — skipping 1 broadcast(s)" in _log(paths)


def test_ledger_loop_logs_failed_sends(env, monkeypatch):
    paths, _ = env
    _ledger(paths, [{"kind": "cleanup", "key": f"c{i}", "outcome": "verified"} for i in range(2)])
    monkeypatch.setattr(listen, "LEDGER_MAX_PER_PASS", 1)
    monkeypatch.setattr(listen, "cli", lambda argv, timeout=30, env=None: (1, "", "slack down"))
    listen.ledger_loop(_one_pass(monkeypatch), {})
    log = _log(paths)
    assert "ledger broadcast rc=1 key=c0 err=slack down" in log
    assert "broadcast overflow summary for 1 update(s) rc=1" in log


def test_mirror_sent_skips_muted_and_unparseable_lines(env):
    paths, calls = env
    listen._mirror_sent("not json\n" + json.dumps({"muted": True}) + "\n"
                        + json.dumps({"message_id": "1", "channel": None}), "body")
    assert calls == []


# ─── inbox cooldown ─────────────────────────────────────────────────────────


def test_inbox_should_ping():
    assert listen.inbox_should_ping({"pattern_matched": "Notification"}, None, 100.0, 900)
    assert not listen.inbox_should_ping({"pattern_matched": "Notification"}, 50.0, 100.0, 900)
    assert listen.inbox_should_ping({"pattern_matched": "stranded"}, 50.0, 1000.0, 900)
    assert listen.inbox_should_ping({"pattern_matched": "AskUserQuestion"}, 99.0, 100.0, 900)


def _signal(inbox: Path, name: str, **fields):
    item = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": "workspace_signal",
            "signal_type": "needs_input", "screen_snippet": "", **fields}
    (inbox / f"cmux-{name}.json").write_text(json.dumps(item))


def test_inbox_pings_a_workspace_once_per_window_except_questions(env, monkeypatch):
    """workspace:244 got 21 pings in six hours. Now the second and third signals
    in the window are held back, but a real question still goes through, and
    the window survives a daemon restart (it's on disk)."""
    paths, calls = env
    inbox = listen.INBOX_DIR
    monkeypatch.setattr(cl, "fmt_workspace_signal", lambda item: f"PING {item['pattern_matched']}")
    _signal(inbox, "a1", ws_ref="workspace:244", pattern_matched="Notification")
    _signal(inbox, "a2", ws_ref="workspace:244", pattern_matched="stranded")
    _signal(inbox, "b1", ws_ref="workspace:7", pattern_matched="Notification")
    assert listen._drain_inbox_once({}) == 2
    _signal(inbox, "a3", ws_ref="workspace:244", pattern_matched="Notification")
    _signal(inbox, "a4", ws_ref="workspace:244", pattern_matched="AskUserQuestion")
    assert listen._drain_inbox_once({}) == 1
    assert _sends(calls) == ["PING Notification", "PING Notification", "PING AskUserQuestion"]
    assert list(inbox.glob("cmux-*.json")) == [], "held-back signals are removed, not retried"
    assert "held back 1 signal(s)" in _log(paths)


def test_cooldown_file_prunes_old_entries_and_tolerates_corruption(env):
    paths, _ = env
    listen._write_cooldown(paths, {"workspace:1": 0.0, "workspace:2": 1000.0}, now=1100.0)
    assert listen._read_cooldown(paths) == {"workspace:2": 1000.0}
    (paths.comms_dir / "inbox-cooldown.json").write_text("[1, 2]")
    assert listen._read_cooldown(paths) == {}
    (paths.comms_dir / "inbox-cooldown.json").write_text("{bad")
    assert listen._read_cooldown(paths) == {}
    (paths.comms_dir / "inbox-cooldown.json").write_text(json.dumps({"a": "x", "b": 5}))
    assert listen._read_cooldown(paths) == {"b": 5}
