#!/bin/zsh
# Launcher for comms-listen.py (the Slack comms daemon).
#
# Runs under zsh (not bash) on purpose: ~/.zprofile is a zsh file and sourcing
# it from bash aborts on zsh-only syntax. zsh sources its own profile cleanly.
# launchd does NOT source ~/.zprofile for us, so without this the Slack token
# ($SLACK_BOT_TOKEN), the optional $SLACK_PING_TARGET override, and the Bedrock
# auth vars the warm session needs would all be missing.
#
# The token is NEVER written into the plist or this script — it comes only from
# ~/.zprofile at runtime. Same pattern as spawn-slack-reactor.sh.
set -euo pipefail

[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1 || true

REPO_DIR="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
PYTHON="$(command -v python3 || echo /opt/homebrew/bin/python3)"

exec "$PYTHON" "$REPO_DIR/bin/comms-listen.py" "$@"
