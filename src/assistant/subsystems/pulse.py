"""PulseSubsystem — drives bin/pulse.py on a fixed interval.

INTEGRATION CHOICE: Option B (subprocess), not Option A (import + call).

Why B, after reading bin/pulse.py:
  - pulse.py does load-bearing work at MODULE IMPORT time, not just in main():
    `logging.basicConfig(filename=PULSE_LOG, ...)` and `_BEDROCK_ENV =
    load_bedrock_env()`. Importing it into the daemon process would hijack the
    daemon's root logger (basicConfig is process-global and first-call-wins)
    and bind every HOME-derived constant at import. The pulse test-suite even
    relies on this — it re-imports the module per test with a fresh HOME.
  - pulse.main() is a SINGLE pulse, not a loop: the cadence lives in the
    LaunchAgent's StartInterval, and main() parses sys.argv via argparse. So
    "call its main loop function" (Option A's phrasing) doesn't map — there is
    no loop function, and calling main() in-process would still need argv
    manipulation + would inherit the daemon's already-configured logging.
  - The pulse spawns its own Observer subprocesses, writes its own heartbeat,
    and is the most safety-critical component. A subprocess gives us complete
    isolation and byte-for-byte compatibility with the system that runs today:
    the daemon runs EXACTLY `python3 bin/pulse.py`, the same command the
    com.assistant.assistant-pulse LaunchAgent runs.

So this subsystem is a clean supervisor loop: run one pulse, sleep
`pulse_interval_sec`, repeat — interruptible on shutdown. Bedrock env is merged
in the same way pulse.py expects (it self-reads ~/.zprofile, but we also pass
our environment through).
"""
from __future__ import annotations

import subprocess
import sys
import time

from . import Subsystem

# A pulse spawns parallel Observer subprocesses (OBSERVER_TIMEOUT_SEC=600 each)
# plus a lesson-extractor pass; give the whole run generous headroom before we
# consider it hung and move on. The next interval will retry regardless.
PULSE_RUN_TIMEOUT_SEC = 1800


class PulseSubsystem(Subsystem):
    name = "pulse"

    def __init__(self, *args, dry_run: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.dry_run = dry_run
        self._runs = 0
        self._last_rc: int | None = None
        self._last_run_ts: int | None = None

    def run(self) -> None:
        self.log.info("pulse subsystem started (interval=%ss, dry_run=%s)",
                      self.config.pulse_interval_sec, self.dry_run)
        # Fire the first pulse immediately (mirrors RunAtLoad), then on cadence.
        while not self.stop.is_set():
            self._run_one_pulse()
            if self.wait(self.config.pulse_interval_sec):
                break
        self.log.info("pulse subsystem stopped (%d run(s))", self._runs)

    def _run_one_pulse(self) -> None:
        script = self.config.pulse_script
        if not script.exists():
            self.log.error("pulse script missing at %s — skipping run", script)
            return
        cmd = [sys.executable, str(script)]
        if self.dry_run:
            cmd.append("--dry-run")
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=PULSE_RUN_TIMEOUT_SEC,
            )
            rc, err = proc.returncode, proc.stderr
        except subprocess.TimeoutExpired:
            rc, err = 124, f"pulse timed out after {PULSE_RUN_TIMEOUT_SEC}s"
        except Exception as e:  # noqa: BLE001 — supervisor must never die on a pulse
            rc, err = 1, str(e)
        self._runs += 1
        self._last_rc = rc
        self._last_run_ts = int(t0)
        wall = time.time() - t0
        if rc == 0:
            self.log.info("pulse run #%d ok in %.1fs", self._runs, wall)
        else:
            self.log.warning("pulse run #%d rc=%d in %.1fs: %s",
                             self._runs, rc, wall, (err or "").strip()[-300:])

    def status(self) -> dict:
        return {
            "name": self.name,
            "runs": self._runs,
            "last_rc": self._last_rc,
            "last_run_ts": self._last_run_ts,
            "interval_sec": self.config.pulse_interval_sec,
            "dry_run": self.dry_run,
        }
