"""daemon — the single-process DaemonProcess that owns every subsystem.

One process, one thread per subsystem, one shared Config + shutdown Event.
Replaces the pulse-timer LaunchAgent with one binary while leaving every
file-based interface untouched.

Lifecycle:
  start()  — write PID file, start each subsystem thread.
  stop()   — set the shutdown Event, join each thread with a 5s timeout, remove
             the PID file. Idempotent.
A SIGTERM/SIGINT handler (installed in __main__) calls stop().
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .config import Config
from .subsystems import Subsystem
from .subsystems.heartbeat import HeartbeatSubsystem
from .subsystems.pulse import PulseSubsystem
from .subsystems.tools import ToolSubsystem

# Per-thread join budget on shutdown. A subsystem's loop wakes on the stop
# Event near-instantly, so 5s is generous; if one overruns we log and move on
# (daemon threads die with the process anyway).
THREAD_JOIN_TIMEOUT_SEC = 5


class DaemonProcess:
    """Owns the subsystem threads. Build with a Config; call start() then
    block on the main thread; call stop() on signal."""

    def __init__(self, config: Config, *, dry_run: bool = False,
                 log: logging.Logger | None = None,
                 subsystems: list[Subsystem] | None = None):
        self.config = config
        self.dry_run = dry_run
        self.log = log or logging.getLogger("assistant.daemon")
        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._stopped = False

        # Allow injection for tests; otherwise build the standard set. The
        # heartbeat subsystem gets a status_provider closure so its on-disk
        # heartbeat snapshots every other subsystem.
        if subsystems is not None:
            self.subsystems = subsystems
        else:
            self.subsystems = self._build_subsystems()

    def _build_subsystems(self) -> list[Subsystem]:
        common = (self.config, self.stop_event, self.log)
        pulse = PulseSubsystem(*common, dry_run=self.dry_run)
        tools = ToolSubsystem(*common)
        heartbeat = HeartbeatSubsystem(
            *common, status_provider=lambda: self._collect_status(
                exclude="heartbeat"))
        # Order matters only for the status snapshot; threads run concurrently.
        self._named = {"pulse": pulse, "tools": tools, "heartbeat": heartbeat}
        return [pulse, tools, heartbeat]

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._write_pid()
        self.log.info("daemon starting (pid=%d, dry_run=%s) — %d subsystem(s)",
                      os.getpid(), self.dry_run, len(self.subsystems))
        for sub in self.subsystems:
            t = threading.Thread(target=self._run_subsystem, args=(sub,),
                                 name=sub.name, daemon=True)
            t.start()
            self._threads.append(t)

    def _run_subsystem(self, sub: Subsystem) -> None:
        try:
            sub.run()
        except Exception as e:  # noqa: BLE001 — one subsystem crashing must not
            # take down the process; log it and let the others keep running.
            self.log.exception("subsystem %s crashed: %s", sub.name, e)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.log.info("daemon stopping — signalling %d subsystem(s)",
                      len(self._threads))
        self.stop_event.set()
        for t in self._threads:
            t.join(timeout=THREAD_JOIN_TIMEOUT_SEC)
            if t.is_alive():
                self.log.warning(
                    "subsystem thread %s did not exit within %ds — "
                    "abandoning (daemon thread dies with the process)",
                    t.name, THREAD_JOIN_TIMEOUT_SEC)
        self._remove_pid()
        self.log.info("daemon stopped")

    def wait(self) -> None:
        """Block the calling (main) thread until shutdown is requested.
        Returns once stop_event is set (by a signal handler calling stop())."""
        while not self.stop_event.is_set():
            self.stop_event.wait(1)

    # ── status ────────────────────────────────────────────────────────────

    def _collect_status(self, exclude: str | None = None) -> dict:
        out = {}
        for sub in self.subsystems:
            if exclude and sub.name == exclude:
                continue
            try:
                out[sub.name] = sub.status()
            except Exception as e:  # noqa: BLE001
                out[sub.name] = {"name": sub.name, "status_error": str(e)[:120]}
        return out

    def status(self) -> dict:
        return {
            "pid": os.getpid(),
            "dry_run": self.dry_run,
            "started": self._started,
            "subsystems": self._collect_status(),
        }

    # ── PID file ──────────────────────────────────────────────────────────

    def _write_pid(self) -> None:
        p = self.config.pid_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        p = self.config.pid_path
        try:
            # Only remove if it's still OUR pid — never yank another daemon's.
            if p.exists() and p.read_text().strip() == str(os.getpid()):
                p.unlink()
        except OSError:
            pass


def read_pid(config: Config) -> int | None:
    """Return the pid recorded in the PID file, or None."""
    p = config.pid_path
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(pid: int | None) -> bool:
    """True if a process with `pid` exists (signal 0 probe)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True
