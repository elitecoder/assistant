#!/usr/bin/env python3
"""Register a provider-correct cmux session record and resume binding on SessionStart.

cmux's Claude wrapper injects its own hooks, but an agent started without the
wrapper (an absolute `~/.local/bin/claude`, or `claude` ahead of the wrapper on
PATH) gets none. This hook forwards SessionStart to cmux's own
`cmux hooks <provider> session-start` so cmux still records that session (what
`cmux sessions list` and `surface resume show` read), and sets a resume binding
when the wrapper didn't. Supports both Claude Code and Factory Droid.

The hook itself only starts a detached worker and returns, so SessionStart
never waits on cmux. The worker checks each forward's exit code, confirms the
record with `cmux sessions list --session <id>`, and retries with backoff
(RETRY_DELAYS_SEC). A session cmux still lacks after the last try gets one
`cmux-session-forward-fail` entry in ~/.assistant/actions-ledger.jsonl, which
the Assistant's brief and Slack feed surface. Nothing here may break the agent
session: every failure is caught.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Seconds to wait before each forward attempt; about a minute in total.
RETRY_DELAYS_SEC = (0, 2, 5, 15, 30)
# cmux has taken 4.8 s to answer `sessions list` on a busy machine.
CALL_TIMEOUT_SEC = 15


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


def cmux_bin():
    return (
        os.environ.get("CMUX_AGENT_HOOK_CMUX_BIN")
        or os.environ.get("CMUX_FACTORY_HOOK_CMUX_BIN")
        or os.environ.get("CMUX_CLAUDE_HOOK_CMUX_BIN")
        or os.environ.get("CMUX_BUNDLED_CLI_PATH", "cmux")
    )


def forward_once(cmux, provider, payload, run=subprocess.run):
    """Forward SessionStart once; return None once cmux has the record, else why not."""
    sid = payload["session_id"]
    try:
        started = run([cmux, "hooks", provider, "session-start"], input=json.dumps(payload),
                      capture_output=True, text=True, timeout=CALL_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return f"session-start timed out after {CALL_TIMEOUT_SEC}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"session-start failed: {exc}"
    if started.returncode != 0:
        return f"session-start exit {started.returncode}: {started.stderr.strip()[:200]}"
    try:
        listed = run([cmux, "sessions", "list", "--session", sid, "--all", "--json"],
                     capture_output=True, text=True, timeout=CALL_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return f"sessions list timed out after {CALL_TIMEOUT_SEC}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"sessions list failed: {exc}"
    try:
        sessions = json.loads(listed.stdout).get("sessions") if listed.returncode == 0 else None
    except (ValueError, AttributeError):
        sessions = None
    if not isinstance(sessions, list):
        return f"sessions list exit {listed.returncode}: unreadable output"
    if not any(isinstance(row, dict) and row.get("session_id") == sid for row in sessions):
        return "cmux has no record of the session after session-start"
    return None


def forward(cmux, provider, payload, run=subprocess.run, sleep=time.sleep):
    """Retry the forward with backoff; return None on success, else the last reason."""
    reason = None
    for delay in RETRY_DELAYS_SEC:
        sleep(delay)
        reason = forward_once(cmux, provider, payload, run)
        if reason is None:
            return None
    return reason


def record_failure(provider, payload, reason, now=None):
    now = time.time() if now is None else now
    sid = payload["session_id"]
    entry = {
        "ts": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch": int(now),
        "key": f"cmux-session-forward-fail:{sid}",
        "kind": "cmux-session-forward-fail",
        "ws_ref": "(hook)",
        "outcome": "failed",
        "evidence": (
            f"cmux never recorded {provider} session {sid} "
            f"(workspace {os.environ.get('CMUX_WORKSPACE_ID', '?')}, "
            f"surface {os.environ.get('CMUX_SURFACE_ID', '?')}) after "
            f"{len(RETRY_DELAYS_SEC)} tries: {reason}"
        )[:300],
    }
    ledger = Path.home() / ".assistant" / "actions-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as stream:
        stream.write(json.dumps(entry) + "\n")


def set_fallback_binding(cmux, provider, sid, cwd, run=subprocess.run):
    # If the wrapper didn't set a binding, set one ourselves. We can't set
    # autoResume=true via the CLI, but we set the correct resume command so
    # `cmux restore --surface` can respawn this panel.
    if os.environ.get("CMUX_AGENT_LAUNCH_ARGV_B64"):
        return
    default_executable = "droid" if provider == "factory" else "claude"
    executable = os.environ.get("CMUX_AGENT_LAUNCH_EXECUTABLE", default_executable)
    resume_cmd = fallback_resume(provider, executable, sid, cwd)
    name = "Factory Droid" if provider == "factory" else "Claude Code"
    try:
        run(
            [
                cmux, "surface", "resume", "set",
                "--kind", provider,
                "--checkpoint-id", sid,
                "--cwd", cwd,
                "--name", name,
                "--source", "agent-hook",
                "--shell", resume_cmd,
            ],
            capture_output=True, text=True, timeout=CALL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def worker(job, run=subprocess.run, sleep=time.sleep):
    """The detached half: forward with retries, ledger a final failure, bind."""
    cmux, provider, payload = cmux_bin(), job["provider"], job["payload"]
    # When CMUX_AGENT_LAUNCH_ARGV_B64 is set, session-start registers autoResume=true.
    reason = forward(cmux, provider, payload, run, sleep)
    set_fallback_binding(cmux, provider, payload["session_id"], payload["cwd"], run)
    if reason is not None:
        record_failure(provider, payload, reason)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        worker(json.loads(argv[1]))
        return
    if not os.environ.get("CMUX_SURFACE_ID", ""):
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
    hook_payload = dict(payload)
    hook_payload.update({"session_id": sid, "cwd": cwd, "transcript_path": transcript})
    job = json.dumps({"provider": provider, "payload": hook_payload})
    # Detached (own session, no inherited pipes): Claude neither waits for it
    # nor kills it at the hook timeout.
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker", job],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
