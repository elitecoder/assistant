#!/bin/zsh
# Launcher for a Keel connector daemon (github-notifications.py, gmail.py, …).
#
# Copied from the spawn-slack-reactor.sh template (design component 2): runs
# under zsh so ~/.zprofile sources cleanly, which launchd does NOT do for us.
# A connector's AUTH does NOT come from ~/.zprofile — GitHub uses the `gh` CLI
# token and Gmail uses the OAuth token cache the base refreshes in-process (the
# design REJECTS static ~/.zprofile Google tokens as the Bedrock-under-launchd
# 403 hazard). We still source ~/.zprofile only for PATH/locale so `gh` and
# python3 resolve; no secrets are read here or written into the plist.
#
# Usage: spawn-connector.sh <connector-script.py> [args…]
set -euo pipefail

[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1 || true

CONNECTOR="${1:?usage: spawn-connector.sh <connector-script.py> [args…]}"
shift || true

REPO_ROOT="$(cd "$(dirname "${(%):-%x}")/../.." && pwd)"
PY="$(command -v python3.12 || command -v python3 || echo /opt/homebrew/bin/python3)"

exec "$PY" "$REPO_ROOT/bin/connectors/$CONNECTOR" "$@"
