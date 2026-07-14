"""Interactive feature opt-in — safety + persistence gate.

The #1 requirement: the discovery prompt must NEVER hang the headless paths
(pulse self-update, curl|bash, CI). Secondary: answers persist so re-runs don't
nag, explicit flags win, and a walk-away timeout stays UNDECIDED (not a silent
'no'). We test install.sh's real behavior with a sandboxed HOME and non-tty
stdin (the headless contract), plus the answer→persist mapping via the same
bash logic install.sh uses.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"

CLEAN_PATH = ("/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
              "/Applications/cmux.app/Contents/Resources/bin")


def _run_install(extra_env=None, flags=(), timeout=90):
    """Run install.sh (dry-run — no --apply, so no mutation) with a sandbox HOME
    and stdin=/dev/null (non-tty). Bounded by `timeout` — a hang is the failure
    we're guarding against. Returns (rc, stdout)."""
    home = tempfile.mkdtemp(prefix="optin-home-", dir="/tmp")
    (Path(home) / ".assistant").mkdir(parents=True)
    env = {"HOME": home, "PATH": CLEAN_PATH}
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(["bash", str(INSTALL), *flags],
                           capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=timeout)
        return r.returncode, r.stdout, home
    finally:
        pass  # caller inspects `home` state file; tmp dirs are reaped by the OS


# ─── the cardinal rule: headless never hangs ────────────────────────────────

def test_self_update_apply_does_not_hang_and_writes_no_prompt():
    # ASSISTANT_SELF_UPDATE=1 + non-tty stdin → both guard terms false. Must
    # complete (not hang) and never print the discovery header.
    rc, out, home = _run_install(extra_env={"ASSISTANT_SELF_UPDATE": "1"}, flags=["--apply"])
    assert "Optional features" not in out, "self-update must not prompt"


def test_non_tty_apply_does_not_hang_or_prompt():
    rc, out, home = _run_install(flags=["--apply"])
    assert "Optional features" not in out, "non-tty --apply must not prompt (headless default-NO)"


def test_dry_run_never_prompts():
    rc, out, home = _run_install(flags=[])  # no --apply
    assert "Optional features" not in out


# ─── answer → persist mapping (the logic install.sh uses) ───────────────────

# Mirror of install.sh's prompt_yn + persist branches — kept in lockstep with
# the shell so the mapping is pinned without a flaky pty.
_HARNESS = r'''
set -uo pipefail
STATE_FILE="%s"; rm -f "$STATE_FILE"
state_set(){ touch "$STATE_FILE"; local t="$STATE_FILE.t"; { grep -v "^$1=" "$STATE_FILE" 2>/dev/null||true; printf '%%s=%%s\n' "$1" "$2"; }>"$t" && mv "$t" "$STATE_FILE"; }
PROMPT_TIMED_OUT=0
prompt_yn(){ local ans rc; PROMPT_TIMED_OUT=0; if read -r -t 60 ans; then rc=0; else rc=$?; fi; [[ ${rc:-0} -gt 128 ]] && { PROMPT_TIMED_OUT=1; ans=""; }; case "$ans" in [yY]|[yY][eE][sS]) return 0;; *) return 1;; esac; }
if prompt_yn; then state_set memory yes; else [[ $PROMPT_TIMED_OUT -eq 1 ]] || state_set memory no; fi
cat "$STATE_FILE" 2>/dev/null || true
'''


def _map(answer: str) -> str:
    sf = tempfile.mktemp(prefix="optin-state-", dir="/tmp")
    script = _HARNESS % sf
    r = subprocess.run(["bash", "-c", script], input=answer,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def test_yes_persists_yes():
    assert _map("y\n") == "memory=yes"
    assert _map("yes\n") == "memory=yes"
    assert _map("Y\n") == "memory=yes"


def test_no_persists_no():
    assert _map("n\n") == "memory=no"


def test_bare_enter_persists_no():
    # empty line (just Enter) is an explicit decline → remembered no (stops nag)
    assert _map("\n") == "memory=no"


def test_eof_persists_no():
    # closed stdin (Ctrl-D / EOF) → explicit decline → no. (Not a timeout.)
    assert _map("") == "memory=no"


# ─── flag precedence ────────────────────────────────────────────────────────

def test_explicit_flag_shows_will_load_in_dryrun():
    # --with-memory (dry-run) → memory resolves to "will load" without any prompt
    rc, out, home = _run_install(flags=["--with-memory"])
    assert "com.assistant.memory-sync-pull.plist — enabled" in out or \
           "memory-sync-pull.plist — enabled" in out
    assert "Optional features" not in out  # flag suppresses the prompt
