#!/usr/bin/env python3
"""
Layer 1: Self-registers a resume binding on SessionStart.

Calls cmux hooks claude session-start (which sets autoResume=true when the
cmux Claude wrapper has set CMUX_AGENT_LAUNCH_ARGV_B64). This is the fast
path — if the cmux wrapper ran, the binding is already registered; this
hook ensures it fires even when cmux's own settings-injection didn't include
the session-start hook (e.g. older sessions or wrapper bypass).

As a fallback, if CMUX_AGENT_LAUNCH_ARGV_B64 is not set (claude started
without going through the cmux wrapper), we call cmux surface resume set
with the correct session_id and cwd so the binding exists for Layer 3
(cmux-restore-sessions) to use.

Does nothing outside a cmux terminal (no CMUX_SURFACE_ID).
Silent on any failure — must never break Claude.
"""
import json
import os
import subprocess
import sys


def main():
    surface_id = os.environ.get("CMUX_SURFACE_ID", "")
    if not surface_id:
        return  # Not inside cmux

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    sid = payload.get("session_id", "")
    cwd = payload.get("cwd", os.getcwd())
    transcript = payload.get("transcript_path", "")
    if not sid:
        return

    cmux_bin = os.environ.get("CMUX_CLAUDE_HOOK_CMUX_BIN") or os.environ.get("CMUX_BUNDLED_CLI_PATH", "cmux")

    # Fast path: delegate to cmux's own session-start hook.
    # When CMUX_AGENT_LAUNCH_ARGV_B64 is set, this registers autoResume=true.
    hook_payload = json.dumps({"session_id": sid, "cwd": cwd, "transcript_path": transcript})
    try:
        subprocess.run(
            [cmux_bin, "hooks", "claude", "session-start"],
            input=hook_payload, capture_output=True, text=True, timeout=5
        )
    except Exception:
        pass

    # Fallback: if wrapper didn't set a binding, set one ourselves.
    # We can't set autoResume=true via the CLI, but we set the correct resume
    # command so Layer 3 (cmux-restore-sessions) can find and respawn this panel.
    if not os.environ.get("CMUX_AGENT_LAUNCH_ARGV_B64"):
        claude_bin = os.environ.get("CMUX_AGENT_LAUNCH_EXECUTABLE", "claude")
        resume_cmd = f"cd '{cwd}' && '{claude_bin}' '--resume' '{sid}'"
        try:
            subprocess.run(
                [
                    cmux_bin, "surface", "resume", "set",
                    "--kind", "claude",
                    "--checkpoint-id", sid,
                    "--cwd", cwd,
                    "--name", "Claude Code",
                    "--source", "agent-hook",
                    "--shell", resume_cmd,
                ],
                capture_output=True, text=True, timeout=5
            )
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
