#!/usr/bin/env python3
"""Layer 1: register a provider-correct cmux resume binding on SessionStart.

Supports both Claude Code and Factory Droid.  The hook is deliberately silent
on every failure because a restore helper must never break the agent session.
"""
import json
import os
import shlex
import subprocess
import sys


def normalize_provider(value):
    value = str(value or "").strip().lower()
    if value in {"droid", "factory"}:
        return "factory"
    if value == "claude":
        return "claude"
    return ""


def infer_provider(payload):
    """Return cmux's provider kind (``claude`` or ``factory``)."""
    for key in ("provider", "agent", "agent_type", "agentType", "kind"):
        provider = normalize_provider(payload.get(key))
        if provider:
            return provider

    transcript = str(
        payload.get("transcript_path") or payload.get("transcriptPath") or ""
    ).replace("\\", "/")
    if "/.factory/sessions" in transcript:
        return "factory"
    if "/.claude/" in transcript:
        return "claude"

    provider = normalize_provider(os.environ.get("CMUX_AGENT_LAUNCH_KIND"))
    if provider:
        return provider
    executable = os.path.basename(
        os.environ.get("CMUX_AGENT_LAUNCH_EXECUTABLE", "")
    ).lower()
    if executable == "droid":
        return "factory"
    if executable == "claude":
        return "claude"
    return ""


def fallback_resume(provider, executable, sid, cwd):
    quoted_cwd = shlex.quote(cwd)
    quoted_sid = shlex.quote(sid)
    quoted_executable = shlex.quote(executable)
    if provider == "factory":
        home = os.path.expanduser("~")
        settings = shlex.quote(
            os.path.join(home, ".assistant", "droid-glm-settings.json")
        )
        command = (
            f"cd {quoted_cwd} && {quoted_executable} "
            f"--settings {settings} --auto high"
        )
        lessons_path = os.path.join(home, ".claude", "CLAUDE.md")
        if os.path.isfile(lessons_path):
            command += (
                " --append-system-prompt-file "
                + shlex.quote(lessons_path))
        return command + f" --resume {quoted_sid}"
    return f"cd {quoted_cwd} && {quoted_executable} --resume {quoted_sid}"


def main():
    surface_id = os.environ.get("CMUX_SURFACE_ID", "")
    if not surface_id:
        return  # Not inside cmux

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    sid = payload.get("session_id") or payload.get("sessionId", "")
    cwd = payload.get("cwd", os.getcwd())
    transcript = payload.get("transcript_path") or payload.get("transcriptPath", "")
    if not sid:
        return

    provider = infer_provider(payload)
    if not provider:
        return
    cmux_bin = (
        os.environ.get("CMUX_AGENT_HOOK_CMUX_BIN")
        or os.environ.get("CMUX_FACTORY_HOOK_CMUX_BIN")
        or os.environ.get("CMUX_CLAUDE_HOOK_CMUX_BIN")
        or os.environ.get("CMUX_BUNDLED_CLI_PATH", "cmux")
    )

    # Fast path: delegate to cmux's own session-start hook.
    # When CMUX_AGENT_LAUNCH_ARGV_B64 is set, this registers autoResume=true.
    hook_payload = dict(payload)
    hook_payload.update(
        {"session_id": sid, "cwd": cwd, "transcript_path": transcript}
    )
    try:
        subprocess.run(
            [cmux_bin, "hooks", provider, "session-start"],
            input=json.dumps(hook_payload),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        pass

    # Fallback: if wrapper didn't set a binding, set one ourselves.
    # We can't set autoResume=true via the CLI, but we set the correct resume
    # command so Layer 3 (cmux-restore-sessions) can find and respawn this panel.
    if not os.environ.get("CMUX_AGENT_LAUNCH_ARGV_B64"):
        default_executable = "droid" if provider == "factory" else "claude"
        executable = os.environ.get(
            "CMUX_AGENT_LAUNCH_EXECUTABLE", default_executable
        )
        resume_cmd = fallback_resume(provider, executable, sid, cwd)
        name = "Factory Droid" if provider == "factory" else "Claude Code"
        try:
            subprocess.run(
                [
                    cmux_bin, "surface", "resume", "set",
                    "--kind", provider,
                    "--checkpoint-id", sid,
                    "--cwd", cwd,
                    "--name", name,
                    "--source", "agent-hook",
                    "--shell", resume_cmd,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
