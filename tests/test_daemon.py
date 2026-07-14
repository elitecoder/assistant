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

from assistant.config import Config
from assistant.daemon import DaemonProcess, is_running, read_pid
from assistant.subsystems import Subsystem
from assistant.subsystems.heartbeat import HeartbeatSubsystem


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path: Path) -> Config:
    """A Config rooted entirely under tmp_path."""
    home = tmp_path / "home"
    assistant_dir = home / ".assistant"
    assistant_dir.mkdir(parents=True)
    return Config(
        home=home,
        assistant_dir=assistant_dir,
        repo=Path(__file__).resolve().parent.parent,
        pulse_interval_sec=1,
        heartbeat_check_sec=1,
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
        # (<dir>/.assistant/config.json) roots the whole tree at <dir>/.assistant.
        cfg_path = tmp_path / ".assistant" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({
            "stale_heartbeat_sec": 999,
            "daemon": {"pulse_interval_sec": 120, "heartbeat_check_sec": 30},
        }))
        cfg = Config.load(cfg_path)
        assert cfg.stale_heartbeat_sec == 999
        assert cfg.pulse_interval_sec == 120
        assert cfg.heartbeat_check_sec == 30
        # Derived paths root at the config's dir.
        assert cfg.assistant_dir == (tmp_path / ".assistant").resolve()

    def test_missing_config_yields_defaults(self, tmp_path: Path):
        cfg = Config.load(tmp_path / "nope" / "config.json")
        assert cfg.pulse_interval_sec == 300
        assert cfg.stale_heartbeat_sec == 1200

    def test_corrupt_config_yields_defaults(self, tmp_path: Path):
        p = tmp_path / ".assistant" / "config.json"
        p.parent.mkdir(parents=True)
        p.write_text("{ not json")
        cfg = Config.load(p)
        assert cfg.pulse_interval_sec == 300


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
        assert set(snap["subsystems"]) == {"pulse", "tools", "heartbeat"}

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
