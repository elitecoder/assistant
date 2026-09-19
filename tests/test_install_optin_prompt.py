"""Interactive feature opt-in — safety + persistence gate.

The #1 requirement: the discovery prompt must NEVER hang the headless paths
(pulse self-update, curl|bash, CI). Secondary: answers persist so re-runs don't
nag, explicit flags win, and a walk-away timeout stays UNDECIDED (not a silent
'no'). We test install.sh's real behavior with a sandboxed HOME and non-tty
stdin (the headless contract), plus the answer→persist mapping via the same
bash logic install.sh uses.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from installer_sandbox import assert_recorded_services, installer_env

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"


def _run_install(home: Path, extra_env=None, flags=(), timeout=90):
    """Run the installer with an isolated HOME and recording service commands."""
    env = {**(extra_env or {}), **installer_env(home)}
    (home / ".assistant").mkdir(parents=True)
    if "--apply" not in flags:
        plists = home / "Library/LaunchAgents"
        plists.mkdir(parents=True)
        for template in (REPO / "launchagents").glob("*.plist"):
            (plists / template.name).write_text("<!-- fixture: previous install -->\n")
    r = subprocess.run(["bash", str(INSTALL), *flags],
                       capture_output=True, text=True, env=env,
                       stdin=subprocess.DEVNULL, timeout=timeout)
    assert r.returncode == 0, f"{r.stdout[-2000:]}\n{r.stderr[-1000:]}"
    assert_recorded_services(home, apply="--apply" in flags)
    return r.returncode, r.stdout, home


# ─── the cardinal rule: headless never hangs ────────────────────────────────

def test_installer_commands_are_recorded_not_executed(tmp_path):
    env = installer_env(tmp_path)
    script = r'''
set -eu
test "$(command -v launchctl)" = "$ASSISTANT_TEST_LAUNCHCTL"
test "$(type -t /bin/launchctl)" = function
test "$(type -t /usr/bin/launchctl)" = function
test "$(type -t /Applications/cmux.app/Contents/Resources/bin/cmux)" = function
test "$(type -t mktemp)" = function
test "$(mktemp -d)" = "$TMPDIR/plist-stage"
if launchctl print gui/0/com.assistant.fixture; then exit 99; fi
launchctl bootout gui/0/com.assistant.fixture
/bin/launchctl bootstrap gui/0 "$HOME/Library/LaunchAgents/fixture.plist"
/usr/bin/launchctl load "$HOME/Library/LaunchAgents/fixture.plist"
/Applications/cmux.app/Contents/Resources/bin/cmux hooks factory install --yes
'''
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert_recorded_services(tmp_path, apply=True)
    assert len((tmp_path / "launchctl.log").read_text().splitlines()) == 4
    assert (tmp_path / "cmux.log").read_text() == "hooks factory install --yes\n"


def test_installer_environment_rejects_real_home():
    with pytest.raises(ValueError, match="isolated HOME"):
        installer_env(Path.home())


def test_self_update_apply_does_not_hang_and_writes_no_prompt(tmp_path):
    # ASSISTANT_SELF_UPDATE=1 + non-tty stdin → both guard terms false. Must
    # complete (not hang) and never print the discovery header.
    rc, out, home = _run_install(tmp_path, extra_env={"ASSISTANT_SELF_UPDATE": "1"}, flags=["--apply"])
    assert "Optional features" not in out, "self-update must not prompt"


def test_non_tty_apply_does_not_hang_or_prompt(tmp_path):
    rc, out, home = _run_install(tmp_path, flags=["--apply"])
    assert "Optional features" not in out, "non-tty --apply must not prompt (headless default-NO)"


def test_dry_run_never_prompts(tmp_path):
    rc, out, home = _run_install(tmp_path, flags=[])  # no --apply
    assert "Optional features" not in out


# ─── answer → persist mapping (the logic install.sh uses) ───────────────────

# Mirror of install.sh's prompt_yn + persist branches — kept in lockstep with
# the shell so the mapping is pinned without a flaky pty.
_HARNESS = r'''
set -uo pipefail
STATE_FILE="%s"
state_set(){ touch "$STATE_FILE"; local t="$STATE_FILE.t"; { grep -v "^$1=" "$STATE_FILE" 2>/dev/null||true; printf '%%s=%%s\n' "$1" "$2"; }>"$t" && mv "$t" "$STATE_FILE"; }
PROMPT_TIMED_OUT=0
prompt_yn(){ local ans rc; PROMPT_TIMED_OUT=0; if read -r -t 60 ans; then rc=0; else rc=$?; fi; [[ ${rc:-0} -gt 128 ]] && { PROMPT_TIMED_OUT=1; ans=""; }; case "$ans" in [yY]|[yY][eE][sS]) return 0;; *) return 1;; esac; }
if prompt_yn; then state_set memory yes; else [[ $PROMPT_TIMED_OUT -eq 1 ]] || state_set memory no; fi
cat "$STATE_FILE" 2>/dev/null || true
'''


def _map(answer: str, home: Path) -> str:
    script = _HARNESS % (home / "optin-state")
    r = subprocess.run(["bash", "-c", script], input=answer,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def test_yes_persists_yes(tmp_path):
    assert _map("y\n", tmp_path) == "memory=yes"
    assert _map("yes\n", tmp_path) == "memory=yes"
    assert _map("Y\n", tmp_path) == "memory=yes"


def test_no_persists_no(tmp_path):
    assert _map("n\n", tmp_path) == "memory=no"


def test_bare_enter_persists_no(tmp_path):
    # empty line (just Enter) is an explicit decline → remembered no (stops nag)
    assert _map("\n", tmp_path) == "memory=no"


def test_eof_persists_no(tmp_path):
    # closed stdin (Ctrl-D / EOF) → explicit decline → no. (Not a timeout.)
    assert _map("", tmp_path) == "memory=no"


# ─── flag precedence ────────────────────────────────────────────────────────

def test_explicit_flag_shows_will_load_in_dryrun(tmp_path):
    # --with-memory (dry-run) → memory resolves to "will load" without any prompt
    rc, out, home = _run_install(tmp_path, flags=["--with-memory"])
    assert "com.assistant.memory-sync-pull.plist — enabled" in out or \
           "memory-sync-pull.plist — enabled" in out
    assert "Optional features" not in out  # flag suppresses the prompt
