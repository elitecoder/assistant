#!/usr/bin/env python3
"""comms-pulse — one headless assistant-comms pulse.

Fired by the LaunchAgent `com.assistant.assistant-comms` every 120s. Each
invocation runs ONE pulse via `claude --print` (headless, non-interactive),
then exits. There is no persistent session, no Terminal window, no tty.

Why headless: the comms agent's memory already lives on disk
(conversation.jsonl + cursors + threads), so the context window is
disposable by design. A one-shot per pulse removes the entire AppleScript /
Terminal.app / tty-tracking failure surface. State is durable; the process
is ephemeral.

Pattern mirrors bin/pulse.py's Observer call: parse Bedrock vars out of
~/.zprofile (launchd does not source it), merge onto the subprocess env, run
`claude --print` with the boot prompt on stdin.

Run by hand to test:  bin/comms-pulse.py            (real pulse)
                       bin/comms-pulse.py --dry-run  (skip the claude call)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402

HOME = Path(os.environ["HOME"])
REPO = Path(__file__).resolve().parent.parent
BOOT_PROMPT = REPO / "prompts" / "prompt-assistant-comms-agent.md"
COMMS_DIR = HOME / ".assistant" / "comms"
RUNS_DIR = COMMS_DIR / "pulse-runs"
LOG = COMMS_DIR / "comms-pulse.log"
LOCK_DIR = COMMS_DIR / "comms-pulse.lock"

DEFAULT_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))
DEFAULT_MODEL = os.environ.get(
    "COMMS_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
PULSE_TIMEOUT_SEC = int(os.environ.get("COMMS_PULSE_TIMEOUT_SEC", "240"))


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    COMMS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{utc_iso()}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr)


def load_bedrock_env() -> dict[str, str]:
    """Parse the Bedrock vars out of ~/.zprofile. launchd does not source it,
    so a headless `claude --print` would otherwise 403 against AWS STS.
    Same approach as bin/pulse.py."""
    extracted: dict[str, str] = {}
    zprofile = HOME / ".zprofile"
    if not zprofile.exists():
        return extracted
    keys = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE", "ANTHROPIC_API_KEY")
    pat = re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$')
    for line in zprofile.read_text().splitlines():
        m = pat.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k not in keys:
            continue
        if (v.startswith('"') and v.endswith('"')) or \
           (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        extracted[k] = v
    return extracted


def acquire_lock() -> bool:
    """Atomic mkdir lock so two overlapping pulses never run claude twice.
    A pulse that runs long (claude is slow) must not get double-fired by the
    next 120s tick. Stale lock (>PULSE_TIMEOUT+60s) is reclaimed."""
    COMMS_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_DIR.exists():
        age = time.time() - LOCK_DIR.stat().st_mtime
        if age > PULSE_TIMEOUT_SEC + 60:
            log(f"stale lock ({int(age)}s) — reclaiming")
            try:
                LOCK_DIR.rmdir()
            except OSError:
                pass
    try:
        LOCK_DIR.mkdir()
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    try:
        LOCK_DIR.rmdir()
    except OSError:
        pass


def next_pulse_idx() -> int:
    """Monotonic pulse counter from the comms heartbeat, +1 each run."""
    hb = comms_lib.read_comms_heartbeat(comms_lib.Paths.from_env())
    if hb and isinstance(hb.get("pulse_idx"), int):
        return hb["pulse_idx"] + 1
    return 1


def build_prompt(pulse_idx: int) -> str:
    """The headless turn instruction. The boot prompt is read BY the agent
    (it's in --add-dir scope); we just point at it and say 'one pulse now'."""
    return (
        f"You are assistant-comms, running as a one-shot headless pulse "
        f"(pulse_idx={pulse_idx}). Read {BOOT_PROMPT} in full and execute "
        f"EXACTLY ONE pulse — the five-step routine in that file — then stop. "
        f"Do not loop or wait for more input; this process exits when you "
        f"finish. Your durable memory is ~/.assistant/comms/conversation.jsonl, "
        f"not this context. End by writing your heartbeat with "
        f"pulse_idx={pulse_idx}."
    )


def run_pulse(dry_run: bool = False) -> int:
    paths = comms_lib.Paths.from_env()

    # Refuse to run if comms was never set up.
    if not paths.config.exists():
        log(f"no config at {paths.config} — run assistant-comms-setup.sh first")
        return 1
    if not BOOT_PROMPT.exists():
        log(f"missing boot prompt at {BOOT_PROMPT}")
        return 1

    pulse_idx = next_pulse_idx()
    prompt = build_prompt(pulse_idx)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS_DIR / f"pulse-{pulse_idx}-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(prompt)

    if dry_run:
        log(f"[dry-run] pulse_idx={pulse_idx} would run claude --print; "
            f"prompt at {run_dir}/prompt.txt")
        comms_lib.write_comms_heartbeat(paths, status="dry-run", pulse_idx=pulse_idx)
        return 0

    cmd = [
        DEFAULT_CLAUDE_BIN,
        "--model", DEFAULT_MODEL,
        "--dangerously-skip-permissions",
        "--print",
        "--add-dir", str(REPO),
        "--add-dir", str(HOME / ".assistant"),
        "--add-dir", str(HOME / ".claude"),
        "--add-dir", "/tmp",
    ]

    env = dict(os.environ)
    for k, v in load_bedrock_env().items():
        env.setdefault(k, v)

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=PULSE_TIMEOUT_SEC, env=env,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"timeout after {PULSE_TIMEOUT_SEC}s"
    except Exception as e:  # noqa: BLE001
        rc, out, err = 1, "", str(e)
    wall_ms = int((time.time() - t0) * 1000)

    (run_dir / "stdout.txt").write_text(out or "")
    (run_dir / "stderr.txt").write_text(err or "")
    (run_dir / "meta.json").write_text(json.dumps({
        "rc": rc, "wall_ms": wall_ms, "model": DEFAULT_MODEL,
        "pulse_idx": pulse_idx, "cmd": cmd, "ts": utc_iso(),
    }, indent=2))

    status = "active" if rc == 0 else "error"
    comms_lib.write_comms_heartbeat(
        paths, status=status, pulse_idx=pulse_idx,
        note=f"rc={rc} wall_ms={wall_ms}")
    log(f"pulse_idx={pulse_idx} rc={rc} wall_ms={wall_ms} run={run_dir.name}")
    return 0 if rc == 0 else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="one headless assistant-comms pulse")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="skip the claude call; write a dry-run heartbeat")
    args = ap.parse_args(argv)

    if not acquire_lock():
        log("another comms-pulse is running — skipping this tick")
        return 0
    try:
        return run_pulse(dry_run=args.dry_run)
    finally:
        release_lock()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
