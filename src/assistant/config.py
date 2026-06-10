"""config — the single Config dataclass + path constants for the daemon.

All filesystem paths the daemon touches are derived from a Config instance, so
a test can root the whole tree at a tmp dir by constructing a Config with a
sandboxed `assistant_dir` (or by pointing `Config.load` at a config.json under
a tmp `.../.assistant/config.json`).

The on-disk config.json is optional: a missing file yields defaults (so the
daemon runs with no config at all). When present, we read only the top-level
`stale_heartbeat_sec` knob and an optional `daemon` block for the
daemon-specific cadences. We never write it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

# ─── path constants (module top, per production-quality requirement) ──────────

HOME = Path(os.environ.get("HOME", str(Path.home())))
# repo root: this file is src/assistant/config.py → parents[2] is the repo.
REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "bin"
ASSISTANT_DIR = HOME / ".assistant"

# Daemon-specific cadences (overridable via the config.json `daemon` block).
DEFAULT_PULSE_INTERVAL_SEC = 300
DEFAULT_STALE_HEARTBEAT_SEC = 1200
DEFAULT_HEARTBEAT_CHECK_SEC = 60


@dataclass
class Config:
    """Daemon configuration + the path tree, all rooted at `assistant_dir`.

    Construct directly for tests/dry-run defaults, or via `Config.load(path)`
    to read the live config.json.
    """

    # daemon cadences
    pulse_interval_sec: int = DEFAULT_PULSE_INTERVAL_SEC
    stale_heartbeat_sec: int = DEFAULT_STALE_HEARTBEAT_SEC
    heartbeat_check_sec: int = DEFAULT_HEARTBEAT_CHECK_SEC

    # roots — paths below are derived from these, so a test can sandbox the
    # whole tree by overriding assistant_dir.
    home: Path = field(default=HOME)
    assistant_dir: Path = field(default=ASSISTANT_DIR)
    repo: Path = field(default=REPO)
    config_path: Path | None = None

    # ── derived paths ─────────────────────────────────────────────────────

    @property
    def daemon_heartbeat_path(self) -> Path:
        """The daemon's OWN liveness heartbeat — written by HeartbeatSubsystem."""
        return self.assistant_dir / "daemon-heartbeat.json"

    @property
    def pid_path(self) -> Path:
        return self.assistant_dir / "daemon.pid"

    @property
    def log_path(self) -> Path:
        return self.assistant_dir / "daemon.log"

    @property
    def pulse_script(self) -> Path:
        return self.repo / "bin" / "pulse.py"

    @property
    def tool_dispatch_script(self) -> Path:
        return self.repo / "bin" / "tool-dispatch.py"

    # ── loading ───────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path | None = None, *,
             home: Path | None = None, repo: Path | None = None) -> "Config":
        """Read a config.json. A missing file yields defaults (so the daemon
        runs with no config); a present file overrides the cadence defaults.

        The whole path tree is rooted at the dir that contains config.json — so
        a test config under a tmp `.assistant/config.json` sandboxes every
        derived path.
        """
        home = home or HOME
        repo = repo or REPO
        path = Path(path) if path is not None else (home / ".assistant/config.json")
        path = path.expanduser()

        # Root the tree at config.json's dir (…/.assistant/config.json → …/.assistant).
        assistant_dir = path.resolve().parent

        cfg = cls(home=home, repo=repo, assistant_dir=assistant_dir, config_path=path)
        if not path.exists():
            return cfg

        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cfg
        if not isinstance(raw, dict):
            return cfg

        daemon = raw.get("daemon", {}) if isinstance(raw.get("daemon"), dict) else {}

        return replace(
            cfg,
            stale_heartbeat_sec=int(raw.get("stale_heartbeat_sec",
                                            DEFAULT_STALE_HEARTBEAT_SEC)),
            pulse_interval_sec=int(daemon.get("pulse_interval_sec",
                                              DEFAULT_PULSE_INTERVAL_SEC)),
            heartbeat_check_sec=int(daemon.get("heartbeat_check_sec",
                                               DEFAULT_HEARTBEAT_CHECK_SEC)),
        )
