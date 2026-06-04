#!/bin/zsh
# Launcher for the Slack reaction → /todo watcher (slack-reaction-todo.py).
#
# Runs under zsh (not bash) on purpose: ~/.zprofile is a zsh file and sourcing
# it from bash aborts on zsh-only syntax. zsh sources its own profile cleanly.
#
# launchd does NOT source ~/.zprofile, so the Slack tokens that live there
# (SLACK_BOT_TOKEN, and SLACK_APP_TOKEN once you add it) would be missing.
# This wrapper sources ~/.zprofile first — same pattern as spawn-comms.sh —
# then execs the watcher via uv (inline PEP-723 deps install slack_sdk).
#
# Tokens are NEVER written into the plist or this script. They come only from
# ~/.zprofile at runtime.
#
# Per-machine routing: TODO_EMOJI selects the emoji this machine claims.
# Override it in ~/.zprofile (e.g. export TODO_EMOJI=inbox_tray) so a different
# machine running this same code with a different TODO_EMOJI picks up other
# reactions.
set -euo pipefail

# Pull in SLACK_BOT_TOKEN / SLACK_APP_TOKEN / TODO_EMOJI / CLAUDE_CODE_USE_BEDROCK etc.
[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1 || true

SCRIPT_DIR="$(cd "$(dirname "${(%):-%x}")" && pwd)"
UV="$(command -v uv || echo /opt/homebrew/bin/uv)"

exec "$UV" run "$SCRIPT_DIR/slack-reaction-todo.py" "$@"
