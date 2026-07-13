#!/usr/bin/env python3
"""
Layer 3: cmux-restore-sessions — one-shot session recovery CLI.

Run after a manual cmux restart (or at boot) to respawn Claude Code or
Factory Droid panels whose sessions did not auto-resume.

Algorithm:
1. Read cmux state to enumerate panels with a supported agent kind.
2. For each panel that has a resumeBinding but NO live agent PID:
   a. If autoResume=true, cmux handles it, so skip.
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
import shlex
import subprocess
import sys
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


def is_agent_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


is_claude_alive = is_agent_alive


def pgrep_session(session_id: str) -> bool:
    """Return True if any process has this agent session id in its argv."""
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


def normalize_provider(value):
    value = str(value or "").strip().lower()
    if value in {"factory", "droid"}:
        return "factory"
    if value == "claude":
        return "claude"
    return ""


def infer_ledger_provider(entry):
    if not entry:
        return ""
    for key in ("provider", "agent", "kind"):
        provider = normalize_provider(entry.get(key))
        if provider:
            return provider
    transcript = str(entry.get("transcript_path", "")).replace("\\", "/")
    if "/.factory/sessions" in transcript:
        return "factory"
    argv = entry.get("launch_argv") or []
    if argv and os.path.basename(str(argv[0])).lower() == "droid":
        return "factory"
    return ""


def panel_provider(panel, ledger_entry=None):
    agent = panel.get("terminal", {}).get("agent", {})
    return (
        normalize_provider(agent.get("kind"))
        or infer_ledger_provider(ledger_entry)
        or ""
    )


def get_all_panels(state, ledger=None):
    """Yield panels belonging to Claude/Droid, including ledger-only matches."""
    ledger = ledger or {}
    windows = state.get("windows", [])
    for window in windows:
        tab_mgr = window.get("tabManager", {})
        for ws in tab_mgr.get("workspaces", []):
            for panel in ws.get("panels", []):
                terminal = panel.get("terminal", {})
                agent = terminal.get("agent", {})
                panel_id = panel.get("id", "")
                ledger_entry = (
                    ledger.get(panel_id) or ledger.get(terminal.get("surfaceId", ""))
                )
                kind = normalize_provider(agent.get("kind"))
                rb = terminal.get("resumeBinding") or {}
                has_session = (
                    agent.get("sessionId")
                    or rb.get("checkpointId")
                    or (ledger_entry or {}).get("session_id")
                )
                if has_session and (kind or ledger_entry):
                    yield ws, panel


def _strip_session_args(args):
    filtered = []
    skip_next = False
    value_flags = {"--resume", "-r", "--session-id", "--fork"}
    switch_flags = {"--continue", "-c"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in value_flags:
            skip_next = True
            continue
        if arg in switch_flags:
            continue
        if any(
            arg.startswith(prefix)
            for prefix in ("--resume=", "--session-id=", "--fork=")
        ):
            continue
        filtered.append(arg)
    return filtered


def _replace_option(args, option, value):
    result = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == option:
            skip_next = True
            continue
        if arg.startswith(option + "="):
            continue
        result.append(arg)
    result.extend([option, value])
    return result


def _strip_option(args, option):
    result = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == option:
            skip_next = True
            continue
        if arg.startswith(option + "="):
            continue
        result.append(arg)
    return result


def _launch_argv(agent, ledger_entry):
    ledger_argv = (ledger_entry or {}).get("launch_argv") or []
    if ledger_argv:
        return [str(arg) for arg in ledger_argv]
    launch = agent.get("launchCommand") or {}
    return [str(arg) for arg in launch.get("arguments", [])]


def build_resume_cmd(panel, ledger_entry=None):
    """Build a provider-correct resume command for a panel."""
    terminal = panel.get("terminal", {})
    agent = terminal.get("agent", {})
    rb = terminal.get("resumeBinding", {}) or {}
    provider = panel_provider(panel, ledger_entry)
    if not provider:
        return None, None

    # Prefer ledger session_id (most recent) over state checkpoint
    if ledger_entry and not ledger_entry.get("ended"):
        session_id = (
            ledger_entry.get("session_id")
            or rb.get("checkpointId")
            or agent.get("sessionId", "")
        )
        cwd = (
            ledger_entry.get("cwd")
            or rb.get("cwd")
            or agent.get("workingDirectory")
            or panel.get("directory", os.getcwd())
        )
    else:
        session_id = rb.get("checkpointId") or agent.get("sessionId", "")
        cwd = (
            rb.get("cwd")
            or agent.get("workingDirectory")
            or panel.get("directory", os.getcwd())
        )

    # A previous resume binding is the most exact command. Droid bindings from
    # before the GLM migration are rebuilt so the required runtime settings are
    # always present.
    command = rb.get("command")
    droid_configured = (
        "droid-glm-settings.json" in (command or "")
        and "--append-system-prompt-file" in (command or "")
        and "--auto high" in (command or "")
    )
    if (
        command
        and session_id
        and session_id in command
        and (provider == "claude" or droid_configured)
    ):
        return cwd, command

    if not session_id:
        return None, None

    args = _launch_argv(agent, ledger_entry)
    default_executable = "droid" if provider == "factory" else "claude"
    executable = args[0] if args else default_executable
    preserved = _strip_session_args(args[1:])

    if provider == "factory":
        settings = str(Path.home() / ".assistant" / "droid-glm-settings.json")
        lessons = str(Path.home() / ".claude" / "CLAUDE.md")
        preserved = _replace_option(preserved, "--settings", settings)
        preserved = _replace_option(preserved, "--auto", "high")
        preserved = _strip_option(
            preserved, "--append-system-prompt-file")
        if Path(lessons).is_file():
            preserved.extend(["--append-system-prompt-file", lessons])

    argv = [executable, "--resume", session_id, *preserved]
    cmd = f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}"

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
    parser = argparse.ArgumentParser(description="Restore dead cmux agent sessions")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be done, don't do it"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
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

    for ws, panel in get_all_panels(state, ledger):
        terminal = panel.get("terminal", {})
        agent = terminal.get("agent", {})
        rb = terminal.get("resumeBinding") or {}
        panel_id = panel.get("id", "")
        ws_title = ws.get("customTitle", "")

        ledger_entry = ledger.get(panel_id) or ledger.get(
            terminal.get("surfaceId", "")
        )

        # Check if the agent is alive. cmux state records agent.pid=0 in
        # practice, so primary liveness check is pgrep on the session_id.
        agent_pid = (
            agent.get("pid", 0)
            or (ledger_entry or {}).get("agent_pid", 0)
            or (ledger_entry or {}).get("pid", 0)
        )
        sid = (
            ((ledger_entry or {}).get("session_id")
             if not (ledger_entry or {}).get("ended") else "")
            or rb.get("checkpointId")
            or agent.get("sessionId", "")
        )
        if is_agent_alive(agent_pid) or pgrep_session(sid):
            if args.verbose:
                print(f"ALIVE: {ws_title!r} (pid={agent_pid} sid={sid[:8]})")
            skipped += 1
            continue

        # If autoResume=true, cmux handles it automatically
        if rb.get("autoResume"):
            if args.verbose:
                print(f"AUTO-RESUME: {ws_title!r}, cmux handles this")
            skipped += 1
            continue

        dead += 1
        if args.verbose:
            print(f"DEAD: {ws_title!r} panel={panel_id[:8]}")

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
