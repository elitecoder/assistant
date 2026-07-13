#!/usr/bin/env python3
"""
Layer 2: Append-only session ledger.

Fires on SessionStart and Stop (via the event arg in argv[1]).
Appends one JSON line to ~/.cmux-session-ledger.jsonl per event so that
cmux-restore-sessions can reconstruct the session→workspace mapping even
if cmux state is wiped.

SessionStart line:
  {"event":"start","ts":..., "session_id":"...", "cwd":"...", "transcript_path":"...",
   "provider":"factory", "agent":"droid", "ws_title":"...", "panel_id":"...",
   "workspace_id":"...", "agent_pid":...}

Stop/SessionEnd line:
  {"event":"end","ts":..., "session_id":"...", "provider":"factory",
   "agent":"droid", "agent_pid":...}
"""
import base64
import fcntl
import json
import os
import sys
import time

LEDGER = os.path.expanduser("~/.cmux-session-ledger.jsonl")
LOCK = LEDGER + ".lock"


def normalize_provider(value):
    value = str(value or "").strip().lower()
    if value in {"factory", "droid"}:
        return "factory", "droid"
    if value == "claude":
        return "claude", "claude"
    return "", ""


def infer_provider(payload):
    for key in ("provider", "agent", "agent_type", "agentType", "kind"):
        provider, agent = normalize_provider(payload.get(key))
        if provider:
            return provider, agent

    transcript = str(
        payload.get("transcript_path") or payload.get("transcriptPath") or ""
    ).replace("\\", "/")
    if "/.factory/sessions" in transcript:
        return "factory", "droid"
    if "/.claude/" in transcript:
        return "claude", "claude"

    provider, agent = normalize_provider(os.environ.get("CMUX_AGENT_LAUNCH_KIND"))
    if provider:
        return provider, agent
    executable = os.path.basename(
        os.environ.get("CMUX_AGENT_LAUNCH_EXECUTABLE", "")
    ).lower()
    if executable == "droid":
        return "factory", "droid"
    if executable == "claude":
        return "claude", "claude"
    return "", ""


def agent_pid(provider):
    names = (
        (
            "CMUX_AGENT_PID",
            "CMUX_DROID_PID",
            "CMUX_FACTORY_PID",
            "CMUX_CLAUDE_PID",
        )
        if provider == "factory"
        else (
            "CMUX_AGENT_PID",
            "CMUX_CLAUDE_PID",
            "CMUX_DROID_PID",
            "CMUX_FACTORY_PID",
        )
    )
    for name in names:
        try:
            value = int(os.environ.get(name, 0))
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def launch_argv():
    encoded = os.environ.get("CMUX_AGENT_LAUNCH_ARGV_B64", "")
    if not encoded:
        executable = os.environ.get("CMUX_AGENT_LAUNCH_EXECUTABLE", "")
        return [executable] if executable else []
    try:
        raw = base64.b64decode(encoded).decode()
        return [arg for arg in raw.split("\0") if arg]
    except Exception:
        return []


def append_line(entry: dict):
    os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
    with open(LOCK, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "start"

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    sid = payload.get("session_id") or payload.get("sessionId", "")
    if not sid:
        return

    ts = time.time()
    provider, agent = infer_provider(payload)
    if not provider:
        return
    pid = agent_pid(provider)

    if event == "start":
        # Get workspace title from cmux if possible
        ws_title = ""
        try:
            import subprocess
            result = subprocess.run(
                ["cmux", "identify", "--json"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                ws_title = (
                    info.get("caller", {}).get("workspace", {}).get("title", "")
                )
        except Exception:
            pass

        entry = {
            "event": "start",
            "ts": ts,
            "session_id": sid,
            "provider": provider,
            "agent": agent,
            "cwd": payload.get("cwd", os.getcwd()),
            "transcript_path": (
                payload.get("transcript_path") or payload.get("transcriptPath", "")
            ),
            "ws_title": ws_title,
            "panel_id": os.environ.get("CMUX_PANEL_ID", ""),
            "surface_id": os.environ.get("CMUX_SURFACE_ID", ""),
            "workspace_id": os.environ.get("CMUX_WORKSPACE_ID", ""),
            "launch_argv": launch_argv(),
            "agent_pid": pid,
            "pid": pid,
        }
    else:
        entry = {
            "event": "end",
            "ts": ts,
            "session_id": sid,
            "provider": provider,
            "agent": agent,
            "agent_pid": pid,
            "pid": pid,
        }

    append_line(entry)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
