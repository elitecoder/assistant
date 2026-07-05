"""config — the single Config dataclass + path constants for the daemon.

All filesystem paths the daemon touches are derived from a Config instance, so
a test can root the whole tree at a tmp dir by constructing a Config with a
sandboxed `assistant_dir` (or by pointing `Config.load` at a config.json under
a tmp `.../.assistant/config.json`).

The on-disk config.json is optional: a missing file yields defaults (so the
daemon runs with no config at all). When present, we read the top-level
`stale_heartbeat_sec` / `mute_until_epoch` knobs, an optional `daemon` block for
the daemon-specific cadences, and an optional `slack` block for the comms
routing target + send-gate allowlist. We never write it.

The Slack bot token is NEVER read from config.json — it comes from
$SLACK_BOT_TOKEN in the environment (sourced from ~/.zprofile), matching the
slack-reactor daemon and the bin/slack-*.py CLIs.
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
DEFAULT_LEDGER_POLL_SEC = 2.0
DEFAULT_HEARTBEAT_DEDUP_SEC = 1800


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
    ledger_poll_sec: float = DEFAULT_LEDGER_POLL_SEC
    heartbeat_dedup_sec: int = DEFAULT_HEARTBEAT_DEDUP_SEC

    # slack comms (CommsSubsystem). bot_token is a property (env), never a field.
    target: str = ""
    allowed_targets: tuple[str, ...] = ()
    mute_until_epoch: int = 0

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
    def heartbeat_path(self) -> Path:
        """Assistant's pulse heartbeat — CommsSubsystem READS this to page on stale."""
        return self.assistant_dir / "heartbeat.json"

    @property
    def comms_dir(self) -> Path:
        return self.assistant_dir / "comms"

    @property
    def ledger_path(self) -> Path:
        return self.assistant_dir / "actions-ledger.jsonl"

    @property
    def ledger_cursor_path(self) -> Path:
        """The daemon's OWN ledger byte cursor — deliberately distinct from
        comms-listen.py's `comms/ledger.cursor` so the two can coexist during
        migration without fighting over the same offset."""
        return self.comms_dir / "daemon-ledger.cursor"

    @property
    def conversation_path(self) -> Path:
        return self.comms_dir / "conversation.jsonl"

    @property
    def threads_path(self) -> Path:
        return self.comms_dir / "threads.jsonl"

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

    # ── slack ─────────────────────────────────────────────────────────────

    @property
    def bot_token(self) -> str:
        """The Slack bot token — from $SLACK_BOT_TOKEN, never config.json."""
        return os.environ.get("SLACK_BOT_TOKEN", "")

    @property
    def has_slack(self) -> bool:
        """True when we have everything needed to actually send to Slack: a bot
        token, a routing target, AND that target on the send-gate allowlist."""
        return bool(self.bot_token and self.target
                    and self.target in self.allowed_targets)

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
        sl = raw.get("slack", {}) if isinstance(raw.get("slack"), dict) else {}

        # $SLACK_PING_TARGET overrides the configured target (env-configurable
        # routing, matching bin/comms_lib.Config.load and slack-reactor).
        target = os.environ.get("SLACK_PING_TARGET") or str(sl.get("target", "") or "")
        allowed = tuple(str(t) for t in sl.get("allowed_targets", []) or [])

        return replace(
            cfg,
            stale_heartbeat_sec=int(raw.get("stale_heartbeat_sec",
                                            DEFAULT_STALE_HEARTBEAT_SEC)),
            mute_until_epoch=int(raw.get("mute_until_epoch", 0) or 0),
            pulse_interval_sec=int(daemon.get("pulse_interval_sec",
                                              DEFAULT_PULSE_INTERVAL_SEC)),
            heartbeat_check_sec=int(daemon.get("heartbeat_check_sec",
                                               DEFAULT_HEARTBEAT_CHECK_SEC)),
            ledger_poll_sec=float(daemon.get("ledger_poll_sec",
                                             DEFAULT_LEDGER_POLL_SEC)),
            heartbeat_dedup_sec=int(daemon.get("heartbeat_dedup_sec",
                                               DEFAULT_HEARTBEAT_DEDUP_SEC)),
            target=target,
            allowed_targets=allowed,
        )
