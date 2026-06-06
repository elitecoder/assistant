"""subsystems — the long-running jobs the daemon owns, one thread each.

Every subsystem is a `Subsystem`: it gets the shared Config + a logger + a
shutdown Event, runs a blocking loop in `run()`, and exits promptly when the
Event is set. The DaemonProcess starts each in its own thread and joins them
under a timeout on shutdown.
"""
from __future__ import annotations

import logging
import threading

from ..config import Config


class Subsystem:
    """Base for a daemon subsystem run in its own thread.

    Subclasses implement `run()` as a loop that returns when `self.stop` is
    set. `wait(sec)` is the cooperative sleep — it returns early the instant a
    shutdown is requested, so a SIGTERM never blocks behind a long sleep.
    """

    #: short name used in logs and `status` output.
    name = "subsystem"

    def __init__(self, config: Config, stop: threading.Event,
                 log: logging.Logger):
        self.config = config
        self.stop = stop
        self.log = log

    def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def status(self) -> dict:
        """Cheap, side-effect-free snapshot for the `status` CLI. Overridden
        by subsystems that have something interesting to report."""
        return {"name": self.name}

    # cooperative sleep that wakes immediately on shutdown.
    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True if a shutdown was requested
        during the wait (i.e. the loop should break)."""
        return self.stop.wait(seconds)
