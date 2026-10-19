"""cmux_sessions — the agent sessions cmux records for its own workspaces.

cmux's agent hooks (injected by its Claude wrapper; installed for Factory Droid
by ``cmux hooks factory install``; forwarded by hooks/cmux-auto-resume.py for
agents started without the wrapper) keep one record per agent session under
``~/.cmuxterm/<agent>-hook-sessions.json``. ``cmux sessions list --json --all``
prints every saved record without needing the cmux socket. This module is the
one place that runs it, so readers get typed records instead of raw JSON.

The CLI is ``$CMUX_BIN`` when set (the override every cmux caller in this repo
honors, and what the test suite points at a nonexistent binary), else an
executable ``$CMUX_BUNDLED_CLI_PATH``, else ``cmux`` on PATH, else the app
bundle's copy. Any failure to run or parse it yields ``[]`` and one line on
stderr (captured in the calling daemon's launchd .err log): a missing or broken
cmux must never take down a caller, but it shouldn't look like "no sessions".

cmux misses a session whose SessionStart never reached it, so readers also
take ``registry_fallback``: rows of ``~/.claude/cmux-registry.json`` (written by
mukul-memory's cmux-registry.py SessionStart hook) for sessions cmux has no
record of. world-scanner counts how often that fallback is used; retire it
once that count stays at zero.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

APP_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"
# A busy machine has taken 4.8 s to answer; a scan that times out sees no records.
TIMEOUT_SEC = 15
CMUX = "cmux"
REGISTRY = "registry"

# cmux's agent names for the two agents the fleet drives, mapped to the
# fleet's own provider names (agent_session.CLAUDE / agent_session.DROID).
_PROVIDERS = {"claude": "claude", "factory": "droid", "droid": "droid"}
_AGENT_PROCESSES = {"claude", "droid"}


@dataclass(frozen=True)
class Session:
    session_id: str
    agent: str
    workspace_id: Optional[str]
    surface_id: Optional[str]
    cwd: Optional[str]
    launch_cwd: Optional[str]
    pid: Optional[int]
    transcript_path: Optional[str]
    started_at: Optional[float]
    updated_at: Optional[float]
    active_for_surface: bool
    launch_argv: tuple = ()
    source: str = CMUX

    @property
    def provider(self) -> Optional[str]:
        """``"claude"`` or ``"droid"``; None for agents the fleet doesn't run."""
        return _PROVIDERS.get(self.agent)


def cli_path() -> str:
    override = os.environ.get("CMUX_BIN")
    if override:
        return override
    bundled = os.environ.get("CMUX_BUNDLED_CLI_PATH")
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    return shutil.which("cmux") or APP_CLI


def list_sessions() -> list[Session]:
    """Every session record cmux has saved, active or not."""
    cli = cli_path()
    try:
        result = subprocess.run(
            [cli, "sessions", "list", "--json", "--all"],
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unavailable(cli, exc)
    if result.returncode != 0:
        return _unavailable(cli, f"exit {result.returncode}: {result.stderr.strip()[:200]}")
    try:
        document = json.loads(result.stdout)
    except ValueError:
        return _unavailable(cli, "output is not JSON")
    rows = document.get("sessions") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return _unavailable(cli, "output has no sessions list")
    return [session for session in map(_parse, rows) if session is not None]


def registry_fallback(path: Path, records) -> list[Session]:
    """Registry rows for Claude sessions that `records` (cmux's) don't include.

    The registry keeps the latest SessionStart per cmux workspace: its key is
    the workspace UUID and its ``ts`` the hook time, which stands in for both
    the start and the latest update. A missing or unreadable registry yields [].
    """
    try:
        registry = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(registry, dict):
        return []
    known = {record.session_id for record in records}
    out = []
    for workspace_id, row in registry.items():
        if not isinstance(row, dict):
            continue
        session_id = _text(row.get("session_id"))
        if not session_id or session_id in known:
            continue
        pid = str(row.get("claude_pid") or "")
        ts = row.get("ts")
        recorded = float(ts) if type(ts) in (int, float) else None
        cwd = _text(row.get("cwd"))
        out.append(Session(
            session_id=session_id, agent="claude",
            workspace_id=_text(row.get("workspace_id")) or workspace_id,
            surface_id=_text(row.get("surface_id")) or _text(row.get("panel_id")),
            cwd=cwd, launch_cwd=cwd,
            pid=int(pid) if pid.isdigit() and int(pid) > 0 else None,
            transcript_path=_text(row.get("transcript_path")),
            started_at=recorded, updated_at=recorded,
            active_for_surface=True, source=REGISTRY,
        ))
    return out


def agent_process(pid) -> bool:
    """True when `pid` is a running claude or droid process, not a reused pid."""
    if not pid:
        return False
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.basename(result.stdout.strip()) in _AGENT_PROCESSES


def _unavailable(cli, reason) -> list[Session]:
    print(f"cmux_sessions: `{cli} sessions list` unavailable ({reason})", file=sys.stderr)
    return []


def _parse(row) -> Optional[Session]:
    if not isinstance(row, dict) or not _text(row.get("session_id")):
        return None
    pid = row.get("pid")
    updated = row.get("updated_at_unix")
    argv = row.get("launch_arguments")
    return Session(
        session_id=row["session_id"],
        agent=str(row.get("agent") or "").lower(),
        workspace_id=_text(row.get("workspace_id")),
        surface_id=_text(row.get("surface_id")),
        cwd=_text(row.get("cwd")),
        launch_cwd=_text(row.get("launch_working_directory")),
        pid=pid if type(pid) is int and pid > 0 else None,
        transcript_path=_text(row.get("transcript_path")),
        started_at=_epoch(row.get("started_at")),
        updated_at=float(updated) if type(updated) in (int, float) else None,
        active_for_surface=row.get("active_for_surface") is True,
        launch_argv=tuple(str(arg) for arg in argv) if isinstance(argv, list) else (),
    )


def _text(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _epoch(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()
