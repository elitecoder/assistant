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
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent_session  # noqa: E402
import cmux_sessions  # noqa: E402
import comms_lib  # noqa: E402
from assistant import mode  # noqa: E402

DROID = agent_session.DROID
COPILOT = agent_session.COPILOT

HOME = Path(os.environ.get("HOME", str(Path.home())))
REPO = Path(__file__).resolve().parent.parent

# Same default as comms_session.py — the warm session invokes this exact binary.

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


def check_tmux(cli: str) -> Check:
    # Pro mode keeps sessions in tmux here (assistant.session_backend), so tmux is CORE in cmux's place.
    rc, _out, err = comms_lib.run_cmd([cli, "ping"], timeout=10)
    if rc == 0:
        return Check("tmux", PASS, core=True, detail="sessions are kept in tmux (ping ok)")
    return Check("tmux", FAIL, core=True, detail=f"sessions are kept in tmux, and it doesn't run: {err.strip()}",
                 remedy="install tmux (brew install tmux, or your system's package manager), "
                        "or set ASSISTANT_TMUX_BIN to its path")


def check_cmux() -> Check:
    # cmux is CORE: pulse dispatches work into cmux workspaces, and the warm
    # comms session lives in one. Non-destructive: existence + `cmux ping`.
    cmux = str(comms_lib.Paths.from_env().cmux_bin)
    if cmux_sessions.in_tmux():
        return check_tmux(cmux)
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


def _agent_binary(agent: str) -> str | None:
    path = agent_session.binary(agent, env={**os.environ, "HOME": str(HOME)})
    return path if Path(path).is_file() and os.access(path, os.X_OK) else None


def check_claude_bin() -> Check:
    # The warm comms session spawns the chosen agent's binary. OPTIONAL — only
    # comms needs it.
    agent = agent_session.warm_session_agent()
    name = f"{agent} binary (warm session)"
    found = _agent_binary(agent)
    if found:
        return Check(name, PASS, core=False, detail=found)
    expected = agent_session.binary(agent, env={**os.environ, "HOME": str(HOME)})
    on_path = shutil.which(agent)
    env_var = agent_session._BIN_ENV[agent]
    if on_path:
        return Check(
            name, WARN, core=False,
            detail=f"{expected} missing; {agent} on PATH at {on_path}",
            remedy=f"set {env_var}={on_path}",
        )
    return Check(name, FAIL, core=False,
                 detail=f"{expected} missing and no {agent} on PATH",
                 remedy=f"install the {agent} CLI, or set {env_var} to its path")


def check_bedrock_env() -> Check:
    # The warm session's headless claude needs auth. OPTIONAL.
    agent = agent_session.warm_session_agent()
    if agent != agent_session.CLAUDE:
        return Check("warm-session auth", SKIP, core=False,
                     detail=f"the Slack session runs {agent}, which signs in on its own")
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


MISTYPED_OWNER = (" (SLACK_USER_ID is set, but to something that isn't one: a member ID is a U or W followed by "
                  "capital letters and digits)")


# What the reactor does meanwhile: with no SLACK_USER_ID at all, Pro mode takes any member's reaction as yours; with a mistyped one
# it keeps saving every reaction as Ask me.
REACTOR_SAYS = {
    False: "The Slack reactor can't tell either, so it takes a reaction with your task emoji from any member as yours.",
    True: "The Slack reactor saves every reaction as Ask me until the ID is fixed.",
}

# The reactor reads SLACK_USER_ID when it starts, like the listener, and is its own service.
REACTOR_RESTART = " (and `launchctl kickstart -k gui/$UID/com.assistant.slack-reactor` if you use reactions)"


def check_slack_owner() -> Check:
    """Only the owner's Slack messages are typed into the warm session, once Assistant knows which member that is.
    In a channel it has to be told; until then every member's messages are typed, as before."""
    target, why = _slack_config()
    if target is None:
        return Check("slack owner", SKIP, core=False, detail=why)
    if target[:1] not in ("C", "G"):
        return Check("slack owner", PASS, core=False, detail="the target is a person, so their messages are yours")
    env_id = os.environ.get("SLACK_USER_ID", "").strip()
    if re.fullmatch(r"[UW][A-Z0-9]{2,}", env_id):
        return Check("slack owner", PASS, core=False, detail="SLACK_USER_ID names you")
    try:
        configured = str(json.loads(comms_lib.Paths.from_env().config.read_text()).get("slack", {}).get("owner") or "")
    except (OSError, ValueError, AttributeError):
        configured = ""
    if re.fullmatch(r"[UW][A-Z0-9]{2,}", configured.strip()):
        return Check("slack owner", PASS, core=False, detail="slack.owner in ~/.assistant/config.json names you")
    return Check("slack owner", WARN, core=False,
                 detail="the target is a channel and neither SLACK_USER_ID nor slack.owner in ~/.assistant/config.json "
                        "is set to a member ID" + (MISTYPED_OWNER if env_id else "") +
                        ", so Assistant can't tell which messages are yours and types every "
                        "member's messages into the session, as it always has. " + REACTOR_SAYS[bool(env_id)] +
                        " Fine if you are the only person in the channels Assistant is in",
                 remedy="to have only your own messages typed, add `export SLACK_USER_ID=U…` (your member ID: your "
                        "Slack profile, the three dots, Copy member ID) to ~/.zprofile (the service reads that file, "
                        "not ~/.zshrc), then run `launchctl kickstart -k gui/$UID/com.assistant.assistant-comms`" + REACTOR_RESTART)


SLACK_CHECKS = [check_slack_token, check_slack_scopes, check_claude_bin, check_bedrock_env,
                check_slack_owner]


# --------------------------------------------------------------------------- provider / agents checks

def _droid_selected() -> bool:
    # The resolved provider/agent across every seam: warm session, dispatch, or
    # the global llm.provider knob. If ANY resolves to droid, the droid launch
    # preconditions must hold, so we run the droid checks.
    return DROID in (agent_session.warm_agent(), agent_session.dispatch_agent())


def _droid_binary() -> str | None:
    return _agent_binary(DROID)


def check_droid_binary() -> Check:
    if not _droid_selected():
        return Check("droid binary", SKIP, core=False,
                     detail="provider is not droid (llm.provider / dispatch.agent / comms)")
    found = _droid_binary()
    if found:
        return Check("droid binary", PASS, core=False, detail=found)
    return Check(
        "droid binary", FAIL, core=False,
        detail="droid not found on PATH or ~/.local/bin, /opt/homebrew/bin, /usr/local/bin",
        remedy="install Factory droid (curl -fsSL https://app.factory.ai/cli | sh) "
               "so it lands in Factory's default install dir, then re-run",
    )


def check_droid_settings() -> Check:
    if not _droid_selected():
        return Check("droid settings", SKIP, core=False,
                     detail="provider is not droid")
    settings = HOME / ".assistant" / "droid-glm-settings.json"
    try:
        parsed = json.loads(settings.read_text())
    except OSError:
        return Check("droid settings", FAIL, core=False,
                     detail=f"{settings} missing",
                     remedy=f"create {settings} (a JSON object with the droid model settings)")
    except ValueError:
        return Check("droid settings", FAIL, core=False,
                     detail=f"{settings} is not valid JSON",
                     remedy=f"fix the JSON in {settings} so Droid can read it")
    if not isinstance(parsed, dict):
        return Check("droid settings", FAIL, core=False,
                     detail=f"{settings} parsed but is not a JSON object",
                     remedy=f"make {settings} a JSON object of droid settings")
    return Check("droid settings", PASS, core=False, detail=str(settings))


def check_copilot_binary() -> Check:
    if not agent_session.copilot_chosen():
        return Check("copilot binary", SKIP, core=False,
                     detail="provider is not copilot (llm.provider / dispatch.agent / comms)")
    found = _agent_binary(COPILOT)
    if found and agent_session.runs(found):
        source = agent_session.copilot_sign_in_source()
        if source is None:
            return Check("copilot binary", FAIL, core=False,
                         detail=f"{found} runs, but no Copilot account is signed in",
                         remedy="run `copilot login`")
        if agent_session.copilot_sign_in_source(env={}) is None:
            shell_only = ("COPILOT_HOME is set" if source == "account"
                          else "COPILOT_GH_HOST or GH_HOST is set" if source == "gh"
                          else f"{source} is set")
            return Check("copilot binary", WARN, core=False,
                         detail=(f"{found} runs, signed in because {shell_only} in this "
                                 "shell; Assistant's background jobs don't get it, so "
                                 "the Slack session and dispatch run Claude and other "
                                 "Copilot jobs fail"),
                         remedy="run `copilot login` (it signs in ~/.copilot)")
        return Check("copilot binary", PASS, core=False, detail=found)
    return Check(
        "copilot binary", FAIL, core=False,
        detail=(f"{found} --version failed" if found else
                "copilot not found on PATH, ~/.local/bin, /opt/homebrew/bin, "
                "/usr/local/bin, or ~/.nvm"),
        remedy="install GitHub Copilot CLI (npm install -g @github/copilot), "
               "run `copilot login`, or set COPILOT_BIN",
    )


def check_copilot_hooks() -> Check:
    if not agent_session.copilot_chosen():
        return Check("copilot cmux hooks", SKIP, core=False,
                     detail="Copilot isn't picked anywhere")
    if agent_session.copilot_hooks_installed(HOME):
        return Check("copilot cmux hooks", PASS, core=False,
                     detail=str(agent_session.copilot_hooks_file(HOME)))
    return Check(
        "copilot cmux hooks", FAIL, core=False,
        detail=f"{agent_session.copilot_hooks_file(HOME)} is missing; Copilot "
               "sessions won't show on the dashboard",
        remedy="python3 bin/agent_session.py install-copilot-hooks",
    )


def check_claude_alias() -> Check:
    alias = agent_session.claude_alias()
    if alias == agent_session.CLAUDE:
        return Check("claude alias", SKIP, core=False, detail="Claude sessions start with claude")
    kind = agent_session.alias_kind(alias)
    if kind in agent_session._LAUNCHABLE_KINDS:
        return Check("claude alias", PASS, core=False, detail=f"{alias} is defined in your login zsh ({kind})")
    if kind:
        return Check(
            "claude alias", WARN, core=False,
            detail=f"{alias} is a shell {kind} in your login zsh, so Claude sessions won't start",
            remedy="run: python3 bin/assistant-llm.py claude-alias claude",
        )
    return Check(
        "claude alias", WARN, core=False,
        detail=f"{alias} isn't an alias, function, or command in your login zsh; "
               "Claude sessions start with claude instead",
        remedy=f"define {alias} in ~/.zprofile, or run: python3 bin/assistant-llm.py claude-alias claude",
    )


def check_observer() -> Check:
    if mode.observer_enabled():
        return Check("observer", PASS, core=False,
                     detail="on in ~/.assistant/config.json; it runs when the pulse has ASSISTANT_WATCHER=1")
    return Check("observer", SKIP, core=False,
                 detail='off (the default). To turn it on, set {"observer": {"enabled": true}} in '
                        "~/.assistant/config.json and run the pulse with ASSISTANT_WATCHER=1")


AGENT_CHECKS = [check_droid_binary, check_droid_settings, check_copilot_binary,
                check_copilot_hooks, check_claude_alias, check_observer]


# --------------------------------------------------------------------------- runner

def run_checks(only: str = "all") -> list[Check]:
    checks: list[Check] = []
    if only in ("all", "core"):
        checks += [c() for c in CORE_CHECKS]
    if only in ("all", "slack"):
        # The owner check belongs to Pro mode's rule about who may type into the Slack session.
        checks += [c() for c in SLACK_CHECKS if c is not check_slack_owner or mode.is_pro()]
    if only == "all":
        checks += [c() for c in AGENT_CHECKS]
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
