"""__main__ — `python -m assistant` entry point.

Usage:
  python -m assistant                       # run the daemon (all subsystems)
  python -m assistant --config <path>       # use a specific config.json
  python -m assistant --dry-run             # pulse-only style
  python -m assistant status                # print subsystem status JSON and exit

Logging is structured to ~/.assistant/daemon.log (the path comes from Config),
never print() — except the `status` subcommand, which prints JSON to stdout by
design (it's a CLI query, not the daemon).
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys

from .config import Config
from .daemon import DaemonProcess, is_running, read_pid


def _setup_logging(config: Config) -> logging.Logger:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(config.log_path))
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("assistant")
    root.setLevel(logging.INFO)
    # Avoid duplicate handlers if main() is re-entered (tests).
    if not any(isinstance(h, logging.FileHandler)
               and getattr(h, "baseFilename", None) == handler.baseFilename
               for h in root.handlers):
        root.addHandler(handler)
    return root


def _cmd_status(config: Config) -> int:
    """Print a status snapshot without starting subsystems. Reports the live
    daemon's PID/liveness from the PID file + its on-disk heartbeat."""
    pid = read_pid(config)
    snapshot = {
        "config_path": str(config.config_path) if config.config_path else None,
        "assistant_dir": str(config.assistant_dir),
        "pid_file": pid,
        "running": is_running(pid),
        "pulse_interval_sec": config.pulse_interval_sec,
    }
    hb = config.daemon_heartbeat_path
    if hb.exists():
        try:
            snapshot["heartbeat"] = json.loads(hb.read_text())
        except (json.JSONDecodeError, OSError):
            snapshot["heartbeat"] = None
    print(json.dumps(snapshot, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="assistant",
        description="Single-process assistant daemon (pulse + heartbeat).")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "status"],
                    help="run the daemon (default) or print status and exit")
    ap.add_argument("--config", default=None,
                    help="path to config.json (default: ~/.assistant/config.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the pulse in dry-run mode")
    args = ap.parse_args(argv)

    config = Config.load(args.config)

    if args.command == "status":
        return _cmd_status(config)

    log = _setup_logging(config)
    daemon = DaemonProcess(config, dry_run=args.dry_run, log=log)

    def handle_sig(signum, _frame):
        log.info("received signal %d — shutting down", signum)
        daemon.stop()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    daemon.start()
    daemon.wait()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
