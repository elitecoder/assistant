#!/bin/zsh
# assistant-comms-setup.sh — first-run bootstrap for assistant-comms (Slack).
#
# 1. Verify $SLACK_BOT_TOKEN is set (from ~/.zprofile) and valid (auth.test).
# 2. Resolve the routing target: $SLACK_PING_TARGET if set, else prompt for the
#    PRIVATE channel id (C…) the bot was /invite-d to (or a U… user for a DM).
# 3. Write ~/.assistant/config.json with slack.target + slack.allowed_targets
#    (the gate confining the bot to that one channel). Token NEVER written.
# 4. Post a test message via bin/slack-send.py to validate egress + the gate.
# 5. Print the exact hand-load command for the opt-in LaunchAgent.
#
# zsh (not bash): ~/.zprofile is a zsh file. Idempotent; safe to re-run.
set -euo pipefail

HOME_DIR="${HOME}"
CONFIG_PATH="${HOME_DIR}/.assistant/config.json"
REPO_DIR="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
PYTHON="$(command -v python3 || echo /opt/homebrew/bin/python3)"
PLIST="${HOME_DIR}/Library/LaunchAgents/com.assistant.assistant-comms.plist"

mkdir -p "${HOME_DIR}/.assistant" "${HOME_DIR}/.assistant/logs"

# --- 1. token -----------------------------------------------------------------
[ -f "$HOME_DIR/.zprofile" ] && . "$HOME_DIR/.zprofile" >/dev/null 2>&1 || true
if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
    echo "ERROR: SLACK_BOT_TOKEN is not set. Add it to ~/.zprofile:" >&2
    echo "  export SLACK_BOT_TOKEN=xoxb-…" >&2
    echo "(the same bot token slack-reactor uses; scopes below)" >&2
    exit 1
fi

echo "[1/4] Verifying SLACK_BOT_TOKEN via auth.test…"
AUTH_JSON="$(curl -sS -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" https://slack.com/api/auth.test || true)"
if ! printf '%s' "$AUTH_JSON" | grep -q '"ok":true'; then
    echo "ERROR: auth.test failed: ${AUTH_JSON}" >&2
    echo "The bot needs scopes: chat:write + groups:history (private channel)" >&2
    echo "or channels:history (public); for a DM target: chat:write + im:write + im:history." >&2
    echo "(add im:write, im:history too if you target a DM instead of a channel)." >&2
    exit 1
fi
BOT_USER_ID="$(printf '%s' "$AUTH_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("user_id",""))')"
TEAM="$(printf '%s' "$AUTH_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("team",""))')"
echo "     ok — team=${TEAM} bot_user_id=${BOT_USER_ID}"

# --- 2. target ----------------------------------------------------------------
echo
echo "[2/4] Routing target"
existing_target=""
if [ -f "${CONFIG_PATH}" ]; then
    existing_target="$("$PYTHON" -c "import json;print(json.load(open('${CONFIG_PATH}')).get('slack',{}).get('target',''))" 2>/dev/null || echo "")"
fi

if [ -n "${SLACK_PING_TARGET:-}" ]; then
    target="${SLACK_PING_TARGET}"
    echo "     using \$SLACK_PING_TARGET = ${target}"
elif [ -n "${existing_target}" ]; then
    echo "     currently configured: ${existing_target}"
    printf '     Enter a new target (U… user DM, or C…/D… channel) or press Enter to keep: '
    read -r target_input
    target="${target_input:-$existing_target}"
else
    echo "     Where should pings + replies go?"
    echo "       • A PRIVATE channel id (C…) you created and /invite-d the bot to (recommended)"
    echo "       • Your OWN Slack user id (U…) — the bot DMs you instead"
    echo "     Channel id: the channel → ⌄ → About → bottom. Bot MUST be a member."
    printf '     Target: '
    read -r target
fi

if [ -z "${target}" ]; then
    echo "ERROR: no target given" >&2
    exit 1
fi

# --- 3. write config ----------------------------------------------------------
echo
echo "[3/4] Writing ${CONFIG_PATH} (token NOT stored here)…"
# The send-gate allowlist is exactly the resolved target — the mechanical
# guarantee that comms can never message anyone else. Merge into any existing
# config.json so unrelated keys (daemon cadences, etc.) survive.
"$PYTHON" - "$CONFIG_PATH" "$target" <<'PY'
import json, os, sys
path, target = sys.argv[1], sys.argv[2]
raw = {}
if os.path.exists(path):
    try:
        raw = json.load(open(path))
    except Exception:
        raw = {}
raw.setdefault("slack", {})
raw["slack"]["target"] = target
# The allowlist is the gate. Keep it as a single-element set == the target.
raw["slack"]["allowed_targets"] = [target]
raw.setdefault("stale_heartbeat_sec", 1200)
with open(path, "w") as f:
    json.dump(raw, f, indent=2)
os.chmod(path, 0o600)
print(f"     target={target} allowed_targets=[{target}]")
PY

# --- 4. test send -------------------------------------------------------------
echo
echo "[4/4] Sending a test message via slack-send.py…"
if "$PYTHON" "$REPO_DIR/bin/slack-send.py" \
        --channel "$target" --kind info \
        --text "assistant-comms is live — this is a one-time setup test."; then
    echo "     ✓ sent. Check Slack for the test message."
else
    echo "     ✗ send failed — see the error above (gate? scope? bad target?)." >&2
    exit 1
fi

# --- 5. preflight: verify the daemon can actually RUN (scopes, claude, auth) --
# A test-send only proves chat:write. The inbound reply path needs more
# (conversations.history scope, the warm-session claude binary, model auth).
# Run the doctor's slack checks and refuse to advertise "load the daemon" if a
# hard check fails — this is what turns H2's silent runtime failure into a
# caught setup-time error.
echo
echo "[5/5] Preflight (assistant-doctor --only slack)…"
if "$PYTHON" "$REPO_DIR/bin/assistant-doctor.py" --only slack --strict; then
    doctor_ok=1
else
    doctor_ok=0
fi

echo
if [ "$doctor_ok" -eq 1 ]; then
    echo "✅ Setup complete. The comms LaunchAgent is OPT-IN and NOT loaded automatically."
else
    echo "⚠️  Setup wrote config + sent a test message, but a preflight check FAILED above."
    echo "    Fix the ↳ items, re-run this script, and only THEN load the daemon:"
fi
echo "   To start it now and on every boot (load when ready):"
echo "     launchctl bootstrap gui/\$UID ${PLIST}"
echo "   To stop it:"
echo "     launchctl bootout gui/\$UID/com.assistant.assistant-comms"
echo
echo "   Run manually instead: ./bin/spawn-comms-listen.sh"
