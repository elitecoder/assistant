"""Tests for the single-process assistant daemon (src/assistant/).

No network, no real cmux, no claude subprocess. Subsystems are exercised with
fakes; the only subprocess we touch is a stubbed pulse script. Fast (<2s).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from assistant import conversation, ledger, tg
from assistant.config import Config
from assistant.daemon import DaemonProcess, is_running, read_pid
from assistant.subsystems import Subsystem
from assistant.subsystems.comms import CommsSubsystem
from assistant.subsystems.heartbeat import HeartbeatSubsystem


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path: Path) -> Config:
    """A Config rooted entirely under tmp_path, with Telegram configured so the
    send path is exercised (tg.send is stubbed in the tests that need it)."""
    home = tmp_path / "home"
    assistant_dir = home / ".assistant"
    (assistant_dir / "comms").mkdir(parents=True)
    return Config(
        bot_token="TESTTOKEN",
        chat_ids=(42,),
        home=home,
        assistant_dir=assistant_dir,
        repo=Path(__file__).resolve().parent.parent,
        pulse_interval_sec=1,
        heartbeat_check_sec=1,
        ledger_poll_sec=0.05,
        heartbeat_dedup_sec=1800,
    )


@pytest.fixture
def log() -> logging.Logger:
    lg = logging.getLogger("assistant.test")
    lg.addHandler(logging.NullHandler())
    return lg


# ─── REQUIRED: test_config_loads ──────────────────────────────────────────────

class TestConfigLoads:
    def test_config_loads(self, tmp_path: Path):
        # A fixture config.json laid out like the real one
        # (<dir>/.assistant/comms/config.json) roots the whole tree at <dir>/.assistant.
        cfg_path = tmp_path / ".assistant" / "comms" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({
            "telegram": {"bot_token": "abc123", "chat_ids": [11, 22]},
            "stale_heartbeat_sec": 999,
            "mute_until_epoch": 5,
            "daemon": {"pulse_interval_sec": 120, "ledger_poll_sec": 3},
        }))
        cfg = Config.load(cfg_path)
        assert cfg.bot_token == "abc123"
        assert cfg.chat_ids == (11, 22)
        assert cfg.stale_heartbeat_sec == 999
        assert cfg.mute_until_epoch == 5
        assert cfg.pulse_interval_sec == 120
        assert cfg.ledger_poll_sec == 3
        assert cfg.has_telegram is True
        # Derived paths root at the config's grandparent dir.
        assert cfg.assistant_dir == (tmp_path / ".assistant").resolve()
        assert cfg.ledger_path == (tmp_path / ".assistant" / "actions-ledger.jsonl").resolve()

    def test_missing_config_yields_defaults(self, tmp_path: Path):
        cfg = Config.load(tmp_path / "nope" / "comms" / "config.json")
        assert cfg.bot_token == ""
        assert cfg.chat_ids == ()
        assert cfg.pulse_interval_sec == 300
        assert cfg.stale_heartbeat_sec == 1200
        assert cfg.has_telegram is False

    def test_corrupt_config_yields_defaults(self, tmp_path: Path):
        p = tmp_path / ".assistant" / "comms" / "config.json"
        p.parent.mkdir(parents=True)
        p.write_text("{ not json")
        cfg = Config.load(p)
        assert cfg.bot_token == ""
        assert cfg.pulse_interval_sec == 300


# ─── REQUIRED: test_tg_format_action_line ─────────────────────────────────────

class TestTgFormatActionLine:
    def test_tg_format_action_line(self):
        entry = {
            "kind": "merge", "key": "workspace:5-ready_for_merge",
            "ws_ref": "workspace:5", "td": "td-9", "outcome": "verified",
            "verified_via": "observer", "pulse_idx": 348,
            "evidence": "sent '/merge-when-ready' delta=1024",
        }
        s = tg.fmt_action_line(entry)
        assert "[merge]" in s
        assert "ok" in s
        assert "workspace:5-ready_for_merge" in s
        assert "ws=workspace:5" in s
        assert "td=td-9" in s
        assert "pulse=348" in s
        assert "via=observer" in s
        assert "sent &#x27;/merge-when-ready&#x27; delta=1024" not in s  # quotes not entity-escaped
        assert "/merge-when-ready" in s

    def test_screen_read_flagged(self):
        s = tg.fmt_action_line({"kind": "k", "key": "x", "verified_via": "screen_read"})
        assert "(!)screen_read" in s

    def test_html_escaped_in_user_fields(self):
        s = tg.fmt_action_line({"kind": "<scr>", "key": "<x>", "evidence": "<i>x</i>"})
        assert "<scr>" not in s
        assert "&lt;scr&gt;" in s
        assert "&lt;i&gt;x&lt;/i&gt;" in s

    def test_outcome_markers(self):
        for outcome, marker in [("failed", "fail"), ("skipped", "skip"),
                                ("rejected", "rej")]:
            assert marker in tg.fmt_action_line({"outcome": outcome})

    def test_missing_fields_default_dashes(self):
        s = tg.fmt_action_line({})
        assert "[?]" in s and "ws=-" in s and "td=-" in s

    def test_send_uses_injected_poster(self):
        # tg.send must route through the injected poster — never the network.
        captured = {}

        def fake_post(token, method, payload):
            captured["token"] = token
            captured["method"] = method
            captured["payload"] = payload
            return {"message_id": 777, "chat_id": payload["chat_id"]}

        result = tg.send("hello", 42, token="TKN", kind="action", http=fake_post)
        assert result["message_id"] == 777
        assert captured["method"] == "sendMessage"
        assert captured["payload"]["chat_id"] == 42
        assert captured["payload"]["parse_mode"] == "HTML"


# ─── REQUIRED: test_ledger_reader_tails ───────────────────────────────────────

class TestLedgerReaderTails:
    def test_ledger_reader_tails(self, tmp_config: Config):
        lp = tmp_config.ledger_path
        lp.parent.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            ledger.append(lp, {"key": f"k{i}", "kind": "test", "n": i})
        reader = ledger.LedgerReader(lp, tmp_config.ledger_cursor_path)
        last3 = reader.tail(3)
        assert [e["n"] for e in last3] == [2, 3, 4]
        # tail is stateless — does not move the cursor.
        assert reader.read_cursor() == 0

    def test_tail_empty_and_nonpositive(self, tmp_config: Config):
        reader = ledger.LedgerReader(tmp_config.ledger_path,
                                     tmp_config.ledger_cursor_path)
        assert reader.tail(5) == []   # no file yet
        ledger.append(tmp_config.ledger_path, {"key": "k"})
        assert reader.tail(0) == []   # n<=0

    def test_read_new_advances_cursor_and_skips_backlog(self, tmp_config: Config):
        lp = tmp_config.ledger_path
        ledger.append(lp, {"key": "old"})
        reader = ledger.LedgerReader(lp, tmp_config.ledger_cursor_path)
        # First run initializes the cursor at EOF → backlog skipped.
        reader.initialize_cursor_if_missing()
        assert reader.read_new() == []
        ledger.append(lp, {"key": "new1"})
        ledger.append(lp, {"key": "new2"})
        fresh = reader.read_new()
        assert [e["key"] for e in fresh] == ["new1", "new2"]
        assert reader.read_new() == []  # cursor advanced; nothing new

    def test_read_new_skips_malformed(self, tmp_config: Config):
        lp = tmp_config.ledger_path
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("not-json\n\n" + json.dumps({"key": "ok"}) + "\n")
        reader = ledger.LedgerReader(lp, tmp_config.ledger_cursor_path)
        reader.write_cursor(0)
        out = reader.read_new()
        assert out == [{"key": "ok"}]


# ─── REQUIRED: test_daemon_starts_and_stops ───────────────────────────────────

class _SpySubsystem(Subsystem):
    """Records that its loop ran and that it observed the shutdown Event."""
    name = "spy"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = threading.Event()
        self.exited = threading.Event()

    def run(self):
        self.started.set()
        while not self.stop.is_set():
            if self.wait(0.05):
                break
        self.exited.set()

    def status(self):
        return {"name": self.name, "started": self.started.is_set()}


class TestDaemonStartsAndStops:
    def test_daemon_starts_and_stops(self, tmp_config: Config, log):
        stop = threading.Event()
        spies = [_SpySubsystem(tmp_config, stop, log) for _ in range(3)]
        daemon = DaemonProcess(tmp_config, log=log, subsystems=spies)
        # Share the daemon's stop_event with the spies (DaemonProcess owns it).
        for s in spies:
            s.stop = daemon.stop_event

        daemon.start()
        # PID file written with our PID.
        assert read_pid(tmp_config) is not None
        assert is_running(read_pid(tmp_config)) is True

        # All subsystems' loops actually started.
        for s in spies:
            assert s.started.wait(2), "subsystem did not start"

        time.sleep(0.2)  # let them spin a little
        daemon.stop()

        # All exited cleanly within the join window.
        for s in spies:
            assert s.exited.is_set(), "subsystem did not exit on stop"
        # PID file removed on clean stop.
        assert read_pid(tmp_config) is None

    def test_stop_is_idempotent(self, tmp_config: Config, log):
        daemon = DaemonProcess(tmp_config, log=log, subsystems=[])
        daemon.start()
        daemon.stop()
        daemon.stop()  # must not raise
        assert read_pid(tmp_config) is None

    def test_status_snapshot_includes_subsystems(self, tmp_config: Config, log):
        daemon = DaemonProcess(tmp_config, dry_run=True, log=log)
        snap = daemon.status()
        assert snap["dry_run"] is True
        assert set(snap["subsystems"]) == {"pulse", "comms", "tools", "heartbeat"}

    def test_subsystem_crash_does_not_kill_daemon(self, tmp_config: Config, log):
        class Boom(Subsystem):
            name = "boom"
            def run(self):
                raise RuntimeError("kaboom")

        good = _SpySubsystem(tmp_config, threading.Event(), log)
        daemon = DaemonProcess(tmp_config, log=log,
                               subsystems=[Boom(tmp_config, threading.Event(), log), good])
        good.stop = daemon.stop_event
        daemon.start()
        assert good.started.wait(2)  # the good one still ran
        daemon.stop()
        assert good.exited.is_set()


# ─── CommsSubsystem broadcast/suppression rules ───────────────────────────────

class TestCommsSubsystem:
    def _comms(self, cfg, log, sent):
        sub = CommsSubsystem(cfg, threading.Event(), log, send_enabled=True)
        # Stub the network: record every (text, chat_id, kind).

        def fake_send(text, chat_id, *, token, kind="reply", **kw):
            sent.append({"text": text, "chat_id": chat_id, "kind": kind})
            return {"message_id": len(sent), "chat_id": chat_id}

        sub._send = fake_send  # not used directly; we monkeypatch tg.send below
        return sub

    def test_suppresses_noop_emit_card_and_self_update_skip(self, tmp_config, log, monkeypatch):
        sent = []
        monkeypatch.setattr(tg, "send",
                            lambda text, chat_id, **kw: sent.append((kw.get("kind"), text)) or {"message_id": 1})
        sub = CommsSubsystem(tmp_config, threading.Event(), log, send_enabled=True)

        sub._broadcast_entry({"kind": "noop", "key": "x", "outcome": "verified"})
        sub._broadcast_entry({"kind": "emit-card", "key": "y", "outcome": "verified"})
        sub._broadcast_entry({"kind": "self-update", "key": "self-update-skip-dirty-p3",
                              "outcome": "verified"})
        sub._broadcast_entry({"kind": "merge", "key": "z", "outcome": "skipped"})
        assert sent == [], "routine/skip entries must not broadcast"

    def test_broadcasts_real_action_and_mirrors_conversation(self, tmp_config, log, monkeypatch):
        sent = []
        monkeypatch.setattr(tg, "send",
                            lambda text, chat_id, **kw: sent.append((chat_id, text)) or
                            {"message_id": 555, "chat_id": chat_id})
        sub = CommsSubsystem(tmp_config, threading.Event(), log, send_enabled=True)
        sub._broadcast_entry({"kind": "merge", "key": "workspace:5-ready_for_merge",
                              "ws_ref": "workspace:5", "outcome": "verified",
                              "verified_via": "observer", "pulse_idx": 1,
                              "evidence": "queued"})
        assert len(sent) == 1
        assert sent[0][0] == 42
        # Mirrored into conversation.jsonl as an out turn.
        rows = conversation.read_window(tmp_config.conversation_path, 42,
                                        now=lambda: time.time() + 1)
        assert len(rows) == 1
        assert rows[0]["direction"] == "out"
        assert rows[0]["kind"] == "action"

    def test_send_disabled_when_no_telegram(self, tmp_path, log, monkeypatch):
        cfg = Config(home=tmp_path, assistant_dir=tmp_path / ".assistant")
        (cfg.comms_dir).mkdir(parents=True)
        called = []
        monkeypatch.setattr(tg, "send", lambda *a, **k: called.append(1) or {})
        sub = CommsSubsystem(cfg, threading.Event(), log, send_enabled=True)
        # has_telegram is False → send_enabled collapses to False.
        sub._broadcast_entry({"kind": "merge", "key": "k", "outcome": "verified"})
        assert called == []


# ─── HeartbeatSubsystem writes the daemon's own heartbeat ─────────────────────

class TestHeartbeatSubsystem:
    def test_writes_daemon_heartbeat_with_subsystem_status(self, tmp_config, log):
        sub = HeartbeatSubsystem(tmp_config, threading.Event(), log,
                                 status_provider=lambda: {"pulse": {"runs": 2}})
        sub._write()
        hb = json.loads(tmp_config.daemon_heartbeat_path.read_text())
        assert hb["process"] == "assistant-daemon"
        assert hb["status"] == "running"
        assert hb["pid"] > 0
        assert hb["subsystems"] == {"pulse": {"runs": 2}}

    def test_write_is_atomic_no_tmp_left(self, tmp_config, log):
        sub = HeartbeatSubsystem(tmp_config, threading.Event(), log)
        sub._write()
        assert tmp_config.daemon_heartbeat_path.exists()
        assert not tmp_config.daemon_heartbeat_path.with_suffix(".json.tmp").exists()


# ─── PulseSubsystem drives bin/pulse.py as a subprocess (Option B) ────────────

class TestPulseSubsystem:
    def test_runs_stub_pulse_script_and_records_rc(self, tmp_config, log, monkeypatch):
        from assistant.subsystems.pulse import PulseSubsystem
        # Point the pulse script at a stub that just exits 0.
        stub = tmp_config.assistant_dir / "stub_pulse.py"
        stub.write_text("import sys; sys.exit(0)\n")
        monkeypatch.setattr(type(tmp_config), "pulse_script",
                            property(lambda self: stub))
        sub = PulseSubsystem(tmp_config, threading.Event(), log, dry_run=True)
        sub._run_one_pulse()
        assert sub.status()["runs"] == 1
        assert sub.status()["last_rc"] == 0

    def test_missing_pulse_script_logged_not_fatal(self, tmp_config, log, monkeypatch):
        from assistant.subsystems.pulse import PulseSubsystem
        missing = tmp_config.assistant_dir / "does-not-exist.py"
        monkeypatch.setattr(type(tmp_config), "pulse_script",
                            property(lambda self: missing))
        sub = PulseSubsystem(tmp_config, threading.Event(), log)
        sub._run_one_pulse()  # must not raise
        assert sub.status()["runs"] == 0
