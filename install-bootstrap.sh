#!/usr/bin/env bash
# install-bootstrap.sh — one-command install for Assistant on a new machine.
#
# Usage (paste into terminal):
#   bash <(curl -fsSL https://raw.githubusercontent.com/elitecoder/assistant/main/install-bootstrap.sh)
#
# What it does:
#   1. Checks prerequisites (git, python3, Factory Droid CLI)
#   2. Clones the repo to ~/dev/assistant (or pulls if already present)
#   3. Runs install.sh --apply  (symlinks skills, copies plists)
#   4. Prints next steps (manual launchctl load)
#
# Idempotent — safe to re-run for updates.

set -euo pipefail

REPO_URL="https://github.com/elitecoder/assistant.git"
REPO_DIR="${HOME}/dev/assistant"

info()  { printf '\033[0;34m==> %s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m    ✓ %s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m    ! %s\033[0m\n' "$*" >&2; }
die()   { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- prereqs
info "Checking prerequisites"

command -v git     >/dev/null 2>&1 || die "git not found — install Xcode Command Line Tools: xcode-select --install"
command -v python3 >/dev/null 2>&1 || die "python3 not found — install from https://brew.sh or python.org"
command -v droid   >/dev/null 2>&1 || die "Factory Droid CLI not found — install Droid first"

ok "git, python3, droid all present"

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
  Assistant installed.

  One manual step remaining — load the pulse LaunchAgent so
  the orchestrator starts on boot (and right now):

    launchctl load -w ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DONE
