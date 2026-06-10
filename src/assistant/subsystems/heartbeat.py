"""HeartbeatSubsystem — the daemon's OWN liveness heartbeat.

Distinct from the pulse heartbeat (~/.assistant/heartbeat.json, written by
bin/pulse.py). This one is the *daemon process* saying "I'm alive": it writes
~/.assistant/daemon-heartbeat.json every heartbeat_check_sec with pid + the
per-subsystem status snapshot, so an external watcher (or a future dashboard
panel) can tell the single-process daemon is up and which subsystems are live.

It does not page anyone — this is purely the daemon's self-report.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from . import Subsystem


class HeartbeatSubsystem(Subsystem):
    name = "heartbeat"

    def __init__(self, *args, status_provider=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Callable returning a dict of per-subsystem status; wired by the
        # DaemonProcess so the heartbeat snapshots the whole daemon.
        self._status_provider = status_provider
        self._writes = 0

    def run(self) -> None:
        self.log.info("heartbeat subsystem started")
        # Write once immediately so the file exists the moment we boot.
        self._write()
        while not self.stop.is_set():
            if self.wait(self.config.heartbeat_check_sec):
                break
            self._write()
        self.log.info("heartbeat subsystem stopped (%d write(s))", self._writes)

    def _write(self) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "epoch": int(time.time()),
            "pid": os.getpid(),
            "status": "running",
            "process": "assistant-daemon",
        }
        if self._status_provider is not None:
            try:
                payload["subsystems"] = self._status_provider()
            except Exception as e:  # noqa: BLE001 — never die writing a heartbeat
                payload["subsystems_error"] = str(e)[:200]
        p = self.config.daemon_heartbeat_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, p)
        self._writes += 1

    def status(self) -> dict:
        return {"name": self.name, "writes": self._writes}
