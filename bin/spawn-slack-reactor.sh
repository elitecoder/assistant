#!/bin/zsh
# Launcher for slack-reactor (the Node/bolt reaction → /todo daemon).
#
# Runs under zsh (not bash) on purpose: ~/.zprofile is a zsh file and sourcing
# it from bash aborts on zsh-only syntax. zsh sources its own profile cleanly.
# launchd does NOT source ~/.zprofile for us, so without this the Slack tokens
# (SLACK_USER_TOKEN / SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET) and TODO_EMOJI
# would be missing.
#
# Tokens are NEVER written into the plist or this script. They come only from
# ~/.zprofile at runtime.
#
# Installs npm deps on first run if node_modules is absent (idempotent).
set -euo pipefail

[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1 || true

PKG_DIR="$(cd "$(dirname "${(%):-%x}")/../slack-reactor" && pwd)"
NODE="$(command -v node || echo /opt/homebrew/bin/node)"
NPM="$(command -v npm || echo /opt/homebrew/bin/npm)"

if [ ! -d "$PKG_DIR/node_modules" ]; then
  echo "[spawn-slack-reactor] installing deps in $PKG_DIR" >&2
  ( cd "$PKG_DIR" && "$NPM" install --no-audit --no-fund )
fi

exec "$NODE" "$PKG_DIR/src/index.js" "$@"
