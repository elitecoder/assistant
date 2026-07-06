#!/usr/bin/env bash
# install-bootstrap.sh — one-command install for Assistant on a new machine.
#
# Usage (paste into terminal):
#   bash <(curl -fsSL https://raw.githubusercontent.com/elitecoder/assistant/main/install-bootstrap.sh)
#
# What it does:
#   1. Checks prerequisites (git, python3, claude CLI)
#   2. Clones the repo to ~/dev/assistant (or pulls if already present)
#   3. Runs install.sh --apply  (symlinks skills, copies plists)
#   4. Prints next steps (manual launchctl load)
#
# Idempotent — safe to re-run for updates.

set -euo pipefail

# Overridable so an engineer with repo access can clone over their own SSH
# identity (the repo is a private, personal-account repo — the public HTTPS
# default only works if it's public or you're already authenticated). E.g.:
#   ASSISTANT_REPO_URL=git@github.com:elitecoder/assistant.git bash install-bootstrap.sh
# NOTE: do NOT hardcode the author's `git@github-personal:` SSH host-alias — it
# is defined only in the author's ~/.ssh/config and resolves nowhere else.
REPO_URL="${ASSISTANT_REPO_URL:-https://github.com/elitecoder/assistant.git}"
REPO_DIR="${ASSISTANT_REPO_DIR:-${HOME}/dev/assistant}"

info()  { printf '\033[0;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m    ✓ %s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m    ! %s\033[0m\n' "$*" >&2; }
die()   { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- prereqs
info "Checking prerequisites"

command -v git     >/dev/null 2>&1 || die "git not found — install Xcode Command Line Tools: xcode-select --install"
command -v python3 >/dev/null 2>&1 || die "python3 not found — install from https://brew.sh or python.org"
command -v claude  >/dev/null 2>&1 || die "claude CLI not found — install from https://claude.ai/code"

ok "git, python3, claude all present"

# uv is strongly preferred but not required
if command -v uv >/dev/null 2>&1; then
    ok "uv found"
else
    warn "uv not found — using python3 -m venv (slower). Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# --------------------------------------------------------------------------- clone / pull
mkdir -p "${HOME}/dev"

if [ -d "${REPO_DIR}/.git" ]; then
    info "Repo already at ${REPO_DIR} — pulling latest"
    git -C "${REPO_DIR}" pull --ff-only || {
        warn "pull failed (dirty tree?). Skipping update — existing version will be used."
    }
else
    info "Cloning Assistant to ${REPO_DIR}"
    git clone "${REPO_URL}" "${REPO_DIR}"
fi

ok "repo ready at ${REPO_DIR}"

# --------------------------------------------------------------------------- install
info "Running install.sh --apply"
bash "${REPO_DIR}/install.sh" --apply

# --------------------------------------------------------------------------- done
cat <<'DONE'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Assistant installed — code + plists in place, nothing loaded yet.

  install.sh just printed the ordered activation runbook above:
  load the CORE pulse agent first, then any optional features
  (cmux-watcher, Slack comms, slack-reactor) you want.

  Start core now:
    launchctl load -w ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist

  Full step-by-step guide, incl. Slack comms: ONBOARDING.md
  Health check anytime:                       ./bin/assistant-doctor.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DONE
