#!/usr/bin/env python3
"""assistant-doctor — fail-loud preflight for the Assistant system.

The single source of truth for "is this machine set up to run Assistant?". Runs
a set of non-destructive checks, each classified CORE (the mechanical pulse
orchestrator needs it — a failure blocks install) or OPTIONAL (an opt-in feature
like Slack comms needs it — a failure only warns). Every check prints
PASS / WARN / FAIL / SKIP with an exact remediation command.

Reused three ways:
  - `bin/install.sh` runs it as phase [0] before any mutation (blocks --apply on
    a CORE failure; report-only under ASSISTANT_SELF_UPDATE=1).
  - `bin/assistant-comms-setup.sh` runs the Slack checks so setup refuses to
    print the launch command with a wrong-scope token.
  - `bin/comms-listen.py` calls the check functions at startup so the daemon
    refuses to crash-loop silently.

Pure stdlib. Exit 0 iff no CORE check FAILed (optional failures never set a
nonzero exit unless --strict). `--only slack|core|all` scopes the run.

Usage:
  assistant-doctor.py [--only core|slack|all] [--strict] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402

HOME = Path(os.environ.get("HOME", str(Path.home())))
REPO = Path(__file__).resolve().parent.parent

# Same default as comms_session.py — the warm session invokes this exact binary.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str                 # PASS | WARN | FAIL | SKIP
    core: bool                  # True → a FAIL blocks install; False → warn only
    detail: str = ""
    remedy: str = ""            # exact command / action to fix a FAIL/WARN


# --------------------------------------------------------------------------- core checks

def check_python() -> Check:
    ok = sys.version_info >= (3, 11)
    return Check(
        "python>=3.11", PASS if ok else FAIL, core=True,
        detail=f"{sys.version.split()[0]} at {sys.executable}",
        remedy="" if ok else "install Python 3.11+ (brew install python@3.12) and re-run",
    )


def check_repo_layout() -> Check:
    # The daemons run scripts from this checkout; the dir must be a real repo
    # with the bin/ scripts present.
    pulse = REPO / "bin" / "pulse.py"
    ok = pulse.is_file()
    return Check(
        "repo layout", PASS if ok else FAIL, core=True,
        detail=f"repo at {REPO}",
        remedy="" if ok else f"expected bin/pulse.py under {REPO}; re-clone the repo",
    )


def check_git() -> Check:
    ok = shutil.which("git") is not None
    return Check(
        "git on PATH", PASS if ok else FAIL, core=True,
        detail=shutil.which("git") or "not found",
        remedy="" if ok else "xcode-select --install",
    )


def check_cmux() -> Check:
    # cmux is CORE: pulse dispatches work into cmux workspaces, and the warm
    # comms session lives in one. Non-destructive: existence + `cmux ping`.
    cmux = str(comms_lib.Paths.from_env().cmux_bin)
    if not Path(cmux).exists():
        return Check("cmux app", FAIL, core=True, detail=f"missing at {cmux}",
                     remedy="install cmux.app, or set CMUX_BIN to its cmux binary")
    rc, _out, err = comms_lib.run_cmd([cmux, "ping"], timeout=10)
    if rc == 0:
        return Check("cmux app", PASS, core=True, detail=f"{cmux} (ping ok)")
    return Check("cmux app", WARN, core=True,
                 detail=f"{cmux} present but `cmux ping` rc={rc}",
                 remedy="launch cmux.app so its socket is up, then re-run")


CORE_CHECKS = [check_python, check_repo_layout, check_git, check_cmux]


# --------------------------------------------------------------------------- slack / warm-session checks (OPTIONAL)

def _slack_config() -> tuple[str | None, str]:
    """Return (target, source) or (None, reason) — reads config.slack.target,
    honoring $SLACK_PING_TARGET. No exception on missing config."""
    try:
        cfg = comms_lib.Config.load(comms_lib.Paths.from_env().config)
    except SystemExit:
        return None, "no ~/.assistant/config.json (run assistant-comms-setup.sh)"
    if not cfg.target:
        return None, "config.slack.target not set (run assistant-comms-setup.sh)"
    return cfg.target, "config"


def _required_scopes(target: str) -> set[str]:
    """The MINIMUM *definite* scopes the daemon's Slack calls need, by target
    type. The history scope is an EITHER/OR set handled separately by
    _history_scopes() (a channel may be public or private).

    Daemon calls (bin/slack-send.py, bin/slack-poll.py) and their scopes:
      send a message        → chat:write        (always)
      conversations.open    → im:write          (only U… user targets — open a DM)
      conversations.history → see _history_scopes(target)
    Deliberately minimal. NOT required: users:read (no daemon users.info call —
    only the separate slack-reactor Node app calls it, with a graceful fallback),
    and groups:read (no conversations.info/list call). An over-broad set produces
    false FAILs, as harmful as false PASSes."""
    base = {"chat:write"}
    if target.startswith("U"):          # user id → DM opened via conversations.open
        return base | {"im:write"}
    return base                          # C…/G…/D… are already channel ids


def _history_scopes(target: str) -> set[str]:
    """Acceptable scopes for conversations.history on this target — ANY ONE
    satisfies. Depends on the channel kind the id denotes:
      U… (→ opened DM) or D… (DM channel id) → im:history
      G… (legacy private group)              → groups:history
      C… (public OR private channel)         → channels:history OR groups:history"""
    if target.startswith(("U", "D")):
        return {"im:history"}
    if target.startswith("G"):
        return {"groups:history"}
    return {"channels:history", "groups:history"}   # C… — either works


def _fetch_scopes(token: str) -> tuple[set[str] | None, str]:
    """auth.test and read the X-OAuth-Scopes response header (non-destructive —
    no message sent, no extra API surface). Returns (scopes, error)."""
    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        data=b"", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            header = resp.headers.get("x-oauth-scopes", "")
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    if not body.get("ok"):
        return None, f"auth.test: {body.get('error')}"
    scopes = {s.strip() for s in header.split(",") if s.strip()}
    return scopes, ""


def check_slack_token() -> Check:
    tok = comms_lib.bot_token()
    if not tok:
        return Check("slack token", SKIP, core=False,
                     detail="$SLACK_BOT_TOKEN unset — Slack comms not configured",
                     remedy="export SLACK_BOT_TOKEN=xoxb-… in ~/.zprofile (only if you want comms)")
    scopes, err = _fetch_scopes(tok)
    if scopes is None:
        return Check("slack token", FAIL, core=False, detail=err,
                     remedy="check $SLACK_BOT_TOKEN is a valid xoxb- bot token")
    return Check("slack token", PASS, core=False, detail="auth.test ok")


def check_slack_scopes() -> Check:
    tok = comms_lib.bot_token()
    if not tok:
        return Check("slack scopes", SKIP, core=False, detail="no token")
    target, src = _slack_config()
    if target is None:
        return Check("slack scopes", SKIP, core=False, detail=src)
    scopes, err = _fetch_scopes(tok)
    if scopes is None:
        return Check("slack scopes", FAIL, core=False, detail=err, remedy="fix the token first")
    need = set(_required_scopes(target))
    missing = need - scopes
    # The history scope is an EITHER/OR set for EVERY target type (a DM needs
    # im:history, a C… channel accepts channels:history OR groups:history, etc.).
    # Flag it only if NONE of the acceptable history scopes is present — this is
    # what closes the D…-target false-PASS (history was previously enforced only
    # for C…).
    hist = _history_scopes(target)
    if not (hist & scopes):
        missing.add(" OR ".join(sorted(hist)))
    if not missing:
        return Check("slack scopes", PASS, core=False,
                     detail=f"target {target}: has {sorted(need)} + a history scope ({sorted(hist & scopes)})")
    return Check(
        "slack scopes", FAIL, core=False,
        detail=f"target {target} MISSING {sorted(missing)}",
        remedy=("add scopes " + ", ".join(sorted(missing)) +
                " at api.slack.com/apps → OAuth → Bot Token Scopes, REINSTALL the app, "
                "then re-run assistant-comms-setup.sh"),
    )


def check_claude_bin() -> Check:
    # The warm comms session spawns this exact binary. OPTIONAL — only comms
    # needs it; the mechanical pulse uses its own claude invocation.
    if Path(CLAUDE_BIN).is_file() and os.access(CLAUDE_BIN, os.X_OK):
        return Check("claude binary (warm session)", PASS, core=False, detail=CLAUDE_BIN)
    on_path = shutil.which("claude")
    if on_path:
        return Check(
            "claude binary (warm session)", WARN, core=False,
            detail=f"{CLAUDE_BIN} missing; claude on PATH at {on_path}",
            remedy=f"symlink it: ln -s {on_path} {CLAUDE_BIN}  (or set CLAUDE_BIN={on_path})",
        )
    return Check("claude binary (warm session)", FAIL, core=False,
                 detail=f"{CLAUDE_BIN} missing and no claude on PATH",
                 remedy="install the Claude Code CLI, then symlink to ~/.local/bin/claude")


def check_bedrock_env() -> Check:
    # The warm session's headless claude needs auth. OPTIONAL.
    env = comms_lib.load_bedrock_env()
    combined = {**env, **os.environ}
    if combined.get("CLAUDE_CODE_USE_BEDROCK", "").strip() not in ("", "0", "false"):
        ok = bool(combined.get("AWS_REGION")) and bool(
            combined.get("AWS_BEARER_TOKEN_BEDROCK") or combined.get("AWS_PROFILE"))
        return Check(
            "warm-session auth (bedrock)", PASS if ok else WARN, core=False,
            detail="bedrock" if ok else "CLAUDE_CODE_USE_BEDROCK set but AWS_REGION / creds incomplete",
            remedy="" if ok else "set AWS_REGION and AWS_BEARER_TOKEN_BEDROCK (or AWS_PROFILE) in ~/.zprofile",
        )
    if combined.get("ANTHROPIC_API_KEY"):
        return Check("warm-session auth (api key)", PASS, core=False, detail="ANTHROPIC_API_KEY set")
    return Check("warm-session auth", WARN, core=False,
                 detail="neither Bedrock nor ANTHROPIC_API_KEY configured",
                 remedy="set ANTHROPIC_API_KEY or the Bedrock vars in ~/.zprofile (only if you want comms)")


SLACK_CHECKS = [check_slack_token, check_slack_scopes, check_claude_bin, check_bedrock_env]


# --------------------------------------------------------------------------- runner

def run_checks(only: str = "all") -> list[Check]:
    checks: list[Check] = []
    if only in ("all", "core"):
        checks += [c() for c in CORE_CHECKS]
    if only in ("all", "slack"):
        checks += [c() for c in SLACK_CHECKS]
    return checks


ICON = {PASS: "✓", WARN: "▲", FAIL: "✗", SKIP: "·"}


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        tag = "core" if c.core else "opt "
        line = f"  {ICON.get(c.status, '?')} [{tag}] {c.name}: {c.status}"
        if c.detail:
            line += f" — {c.detail}"
        lines.append(line)
        if c.status in (FAIL, WARN) and c.remedy:
            lines.append(f"        ↳ fix: {c.remedy}")
    return "\n".join(lines)


def core_failed(checks: list[Check]) -> bool:
    return any(c.core and c.status == FAIL for c in checks)


def any_failed(checks: list[Check]) -> bool:
    return any(c.status == FAIL for c in checks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assistant preflight doctor")
    ap.add_argument("--only", default="all", choices=["all", "core", "slack"])
    ap.add_argument("--strict", action="store_true",
                    help="exit nonzero on ANY failure (incl. optional), not just core")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    checks = run_checks(args.only)

    if args.as_json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        print("assistant-doctor — preflight")
        print(format_report(checks))
        core_bad = core_failed(checks)
        opt_bad = any(not c.core and c.status == FAIL for c in checks)
        if core_bad:
            print("\n✗ CORE checks failed — the pulse orchestrator will not run. Fix the ↳ items above.")
        elif opt_bad:
            print("\n▲ Core is OK. Optional-feature checks failed (Slack comms / warm session) — "
                  "fine to install core; fix those before enabling comms.")
        else:
            print("\n✓ All checks passed.")

    if args.strict:
        return 1 if any_failed(checks) else 0
    return 1 if core_failed(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
