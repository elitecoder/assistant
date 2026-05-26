#!/usr/bin/env python3
"""
Layer 3: cmux-restore-sessions — one-shot session recovery CLI.

Run after a manual cmux restart (or at boot) to respawn Claude panels
whose sessions didn't auto-resume.

Algorithm:
1. Read cmux state to enumerate live panels with a Claude agent kind.
2. For each panel that has a resumeBinding but NO live claude PID:
   a. If autoResume=true, cmux handles it — skip.
   b. If the resumeBinding has a checkpointId (session_id), respawn.
3. For panels with NO resumeBinding at all, fall back to the ledger
   (~/.cmux-session-ledger.jsonl) to find the last session for that panel.
4. Respawn by sending the resume command to the panel's surface.

Usage:
  python3 ~/.claude/bin/cmux-restore-sessions.py [--dry-run] [--verbose]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

LEDGER = Path.home() / ".cmux-session-ledger.jsonl"
CMUX_STATE = Path.home() / "Library/Application Support/cmux/session-com.cmuxterm.app.json"


def run_cmux(*args, timeout=5):
    try:
        r = subprocess.run(
            ["/Applications/cmux.app/Contents/Resources/bin/cmux", *args],
            capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def load_state():
    with open(CMUX_STATE) as f:
        return json.load(f)


def load_ledger():
    """Returns {panel_id: last_start_entry} from the ledger."""
    if not LEDGER.exists():
        return {}
    starts = {}
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("event") == "start":
                panel_id = entry.get("panel_id") or entry.get("surface_id", "")
                if panel_id:
                    existing = starts.get(panel_id)
                    if not existing or entry.get("ts", 0) > existing.get("ts", 0):
                        starts[panel_id] = entry
            elif entry.get("event") == "end":
                # Mark the session as ended
                sid = entry.get("session_id", "")
                for pid, e in list(starts.items()):
                    if e.get("session_id") == sid:
                        starts[pid]["ended"] = True
    return starts


def is_claude_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pgrep_session(session_id: str) -> bool:
    """Returns True if a claude process exists with --resume <session_id>
    OR a session-id-bearing arg matching this session in its argv. Used as a
    fallback when cmux state's agent.pid is 0 (which it always is in
    practice). Without this, Layer 3 risks typing into a live pane."""
    if not session_id:
        return False
    try:
        r = subprocess.run(
            ["pgrep", "-f", session_id],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


def get_all_panels(state):
    """Yields (workspace, panel) tuples for all panels with a Claude agent."""
    windows = state.get("windows", [])
    for window in windows:
        tab_mgr = window.get("tabManager", {})
        for ws in tab_mgr.get("workspaces", []):
            for panel in ws.get("panels", []):
                terminal = panel.get("terminal", {})
                agent = terminal.get("agent", {})
                if agent.get("kind") == "claude" and agent.get("sessionId"):
                    yield ws, panel


def build_resume_cmd(panel, ledger_entry=None):
    """Build the claude --resume command for a panel."""
    terminal = panel.get("terminal", {})
    agent = terminal.get("agent", {})
    rb = terminal.get("resumeBinding", {}) or {}

    # Prefer ledger session_id (most recent) over state checkpoint
    if ledger_entry and not ledger_entry.get("ended"):
        session_id = ledger_entry.get("session_id") or rb.get("checkpointId") or agent.get("sessionId", "")
        cwd = ledger_entry.get("cwd") or rb.get("cwd") or agent.get("workingDirectory") or panel.get("directory", os.getcwd())
    else:
        session_id = rb.get("checkpointId") or agent.get("sessionId", "")
        cwd = rb.get("cwd") or agent.get("workingDirectory") or panel.get("directory", os.getcwd())

    # Use state's command only if it has the right session_id
    command = rb.get("command")
    if command and session_id and session_id in command:
        return cwd, command
    command = None

    if not session_id:
        return None, None

    lc = agent.get("launchCommand", {})
    args = lc.get("arguments", [])
    if args:
        # Reconstruct args, injecting --resume
        filtered = []
        skip_next = False
        for arg in args[1:]:  # skip executable
            if skip_next:
                skip_next = False
                continue
            if arg in ("--resume", "-r", "--session-id", "--continue", "-c"):
                skip_next = True
                continue
            if arg.startswith("--resume=") or arg.startswith("--session-id="):
                continue
            filtered.append(f"'{arg}'")
        exe = f"'{args[0]}'"
        resume_args = " ".join(["--resume", f"'{session_id}'"] + filtered)
        cmd = f"cd '{cwd}' && {exe} {resume_args}"
    else:
        # Minimal fallback
        cmd = f"cd '{cwd}' && claude --resume '{session_id}'"

    return cwd, cmd


def respawn_panel(ws, panel, cmd: str, dry_run: bool, verbose: bool):
    panel_id = panel.get("id", "")
    surface_id = panel.get("terminal", {}).get("surfaceId", panel_id)
    ws_title = ws.get("customTitle", ws.get("title", ""))

    if verbose:
        print(f"  Respawning: ws={ws_title!r} panel={panel_id[:8]}")
        print(f"  Command: {cmd[:100]}...")

    if dry_run:
        print(f"  [DRY-RUN] Would send: {cmd}")
        return True

    # Try to respawn by sending the command to the surface
    rpc_params = json.dumps({"surface_id": surface_id, "text": cmd})
    rc, out, err = run_cmux("rpc", "surface.send_text", rpc_params, timeout=5)
    if rc != 0:
        # Fall back to surface ref
        rc, out, err = run_cmux("rpc", "surface.send_text",
                                json.dumps({"surface_id": panel_id, "text": cmd}), timeout=5)
    if rc == 0:
        # Send Enter
        run_cmux("rpc", "surface.send_key",
                 json.dumps({"surface_id": surface_id or panel_id, "key": "enter"}), timeout=5)
        return True

    if verbose:
        print(f"  ERROR: send_text failed: {err}")
    return False


def main():
    parser = argparse.ArgumentParser(description="Restore dead cmux Claude sessions")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done, don't do it")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    try:
        state = load_state()
    except Exception as e:
        print(f"Error reading cmux state: {e}", file=sys.stderr)
        sys.exit(1)

    ledger = load_ledger()
    if args.verbose:
        print(f"Ledger entries: {len(ledger)}")

    restored = 0
    skipped = 0
    dead = 0

    for ws, panel in get_all_panels(state):
        terminal = panel.get("terminal", {})
        agent = terminal.get("agent", {})
        rb = terminal.get("resumeBinding") or {}
        panel_id = panel.get("id", "")
        ws_title = ws.get("customTitle", "")

        # Check if claude is alive. cmux state records agent.pid=0 in
        # practice, so primary liveness check is pgrep on the session_id.
        agent_pid = agent.get("pid", 0)
        sid = (rb.get("checkpointId")
               or agent.get("sessionId", ""))
        if is_claude_alive(agent_pid) or pgrep_session(sid):
            if args.verbose:
                print(f"ALIVE: {ws_title!r} (pid={agent_pid} sid={sid[:8]})")
            skipped += 1
            continue

        # If autoResume=true, cmux handles it automatically
        if rb.get("autoResume"):
            if args.verbose:
                print(f"AUTO-RESUME: {ws_title!r} — cmux handles this")
            skipped += 1
            continue

        dead += 1
        if args.verbose:
            print(f"DEAD: {ws_title!r} panel={panel_id[:8]}")

        ledger_entry = ledger.get(panel_id) or ledger.get(terminal.get("surfaceId", ""))
        cwd, cmd = build_resume_cmd(panel, ledger_entry)
        if not cmd:
            print(f"  SKIP: no session_id for {ws_title!r}")
            continue

        if respawn_panel(ws, panel, cmd, args.dry_run, args.verbose):
            restored += 1
            print(f"RESTORED: {ws_title!r}")
        else:
            print(f"FAILED: {ws_title!r}")

    print(f"\nSummary: {restored} restored, {skipped} skipped, {dead} dead")


if __name__ == "__main__":
    main()
