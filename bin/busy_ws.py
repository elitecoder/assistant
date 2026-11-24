"""busy_ws — which live cmux workspaces are busy right now, with no model call.

The pulse's cap on busy sessions (pulse.ACTIVE_WS_CAP) must hold whether or not
the Observer runs, so the count cannot lean on the Observer's per-workspace
contexts. This module builds it from what cmux and the transcripts already say:

  1. `cmux tree --all --json` names every live workspace and its UUID.
  2. `cmux sessions list` (plus the registry fallback world-scanner uses) names
     each agent session, the workspace UUID it lives in, its pid and transcript.
  3. A workspace is busy when any of its live agent sessions is, by the one
     shared predicate config.ws_is_active, given the (last turn age, working)
     pair build-ws-context.transcript_signals reads off the tail of the
     transcript. "Working" there means a tool call with no result yet, so a
     session in the middle of a 30-minute tool call counts even though its
     transcript has been quiet for longer than the 10-minute window.

A workspace with no agent session (a plain terminal) is not busy; a session
whose process has exited is not busy. A session whose transcript cannot be
found or read IS busy: not knowing is the case the cap exists to be careful
about, and a wrong "idle" starts one more real agent.

`busy_refs()` raises Unavailable when cmux can't list the workspaces or the
sessions, so the caller can tell "nothing is busy" from "couldn't look".
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(BIN.parent / "src"))
import agent_session  # noqa: E402
import cmux_sessions  # noqa: E402
from assistant import config  # noqa: E402


class Unavailable(RuntimeError):
    """cmux could not say which workspaces or sessions are live."""


def _transcript_signals(path: str):
    """build-ws-context.transcript_signals: the Observer's own reader of a
    transcript tail (bounded to the last 64KB). Loaded by path because the
    script's name has a hyphen; reusing it keeps one parser for "working"."""
    spec = importlib.util.spec_from_file_location(
        "build_ws_context_for_busy_ws", BIN / "build-ws-context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.transcript_signals(path)


def _pid_alive(pid) -> bool:
    # Same test world-scanner applies to cmux's records.
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError, TypeError):
        return False


def _workspace_refs_by_id() -> dict[str, str]:
    """{workspace UUID (lower case): ws_ref} for every live workspace, across
    all windows."""
    try:
        result = subprocess.run(
            [cmux_sessions.cli_path(), "--id-format", "both", "tree", "--all", "--json"],
            capture_output=True, text=True, timeout=cmux_sessions.TIMEOUT_SEC)
        tree = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        tree = None
    if not isinstance(tree, dict):
        raise Unavailable("cmux tree did not answer")
    refs = {}
    for window in tree.get("windows") or []:
        for ws in window.get("workspaces") or []:
            if ws.get("ref") and ws.get("id"):
                refs[str(ws["id"]).lower()] = ws["ref"]
    return refs


def _session_busy(record) -> bool:
    home = os.environ["HOME"]
    path = record.transcript_path or agent_session.transcript_for_session(
        record.provider, record.session_id, home=home)
    if not (path and os.path.isfile(path) and os.access(path, os.R_OK)):
        return True
    age, status = _transcript_signals(path)
    # A transcript that vanished between the check and the read has no age.
    return age is None or config.ws_is_active(status, age)


def busy_refs() -> set[str]:
    """ws_refs of the live workspaces with at least one busy agent session."""
    refs_by_id = _workspace_refs_by_id()
    try:
        records = cmux_sessions.list_sessions(strict=True)
    except cmux_sessions.SessionsUnavailable as exc:
        raise Unavailable(f"cmux sessions list did not answer: {exc}") from exc
    records += cmux_sessions.registry_fallback(
        Path(os.environ["HOME"]) / ".claude/cmux-registry.json", records)
    busy: set[str] = set()
    for record in records:
        ref = refs_by_id.get((record.workspace_id or "").lower())
        if ref is None or ref in busy:
            continue
        # The same "live agent session" filter world-scanner applies.
        if not (record.active_for_surface and record.provider and _pid_alive(record.pid)):
            continue
        if record.source == cmux_sessions.REGISTRY and not cmux_sessions.agent_process(record.pid):
            continue
        if _session_busy(record):
            busy.add(ref)
    return busy
