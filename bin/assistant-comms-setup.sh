#!/usr/bin/env bash
# assistant-comms-setup.sh — first-run bootstrap for assistant-comms.
#
# 1. Create the uv-managed venv at ~/.assistant/comms-venv/.
# 2. Install python-telegram-bot.
# 3. Prompt for the bot token (BotFather), capture chat_id by waiting for /start.
# 4. Write ~/.assistant/comms/config.json (chmod 600).
#
# Idempotent. Safe to re-run.
#
# Does NOT load the LaunchAgent — Mukul does that himself:
#   launchctl load -w ~/Library/LaunchAgents/com.assistant.assistant-comms.plist

set -euo pipefail

HOME_DIR="${HOME}"
VENV_DIR="${HOME_DIR}/.assistant/comms-venv"
CONFIG_DIR="${HOME_DIR}/.assistant/comms"
CONFIG_PATH="${CONFIG_DIR}/config.json"
LOG_DIR="${HOME_DIR}/.architect/orchestrator-logs"

mkdir -p "${CONFIG_DIR}" "${LOG_DIR}"

# --- venv ---------------------------------------------------------------------
if [ ! -x "${VENV_DIR}/bin/python" ]; then
    echo "[1/4] creating venv at ${VENV_DIR}"
    if command -v uv >/dev/null 2>&1; then
        uv venv "${VENV_DIR}"
    else
        python3 -m venv "${VENV_DIR}"
    fi
else
    echo "[1/4] venv already exists at ${VENV_DIR}"
fi

# --- python-telegram-bot ------------------------------------------------------
echo "[2/4] installing python-telegram-bot into venv"
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "${VENV_DIR}/bin/python" 'python-telegram-bot>=21,<22'
else
    "${VENV_DIR}/bin/pip" install --upgrade pip
    "${VENV_DIR}/bin/pip" install 'python-telegram-bot>=21,<22'
fi

# --- bot token ----------------------------------------------------------------
existing_token=""
existing_chats=""
if [ -f "${CONFIG_PATH}" ]; then
    existing_token=$("${VENV_DIR}/bin/python" -c "import json,sys; print(json.load(open('${CONFIG_PATH}')).get('telegram',{}).get('bot_token',''))" 2>/dev/null || echo "")
    existing_chats=$("${VENV_DIR}/bin/python" -c "import json,sys; print(','.join(str(x) for x in json.load(open('${CONFIG_PATH}')).get('telegram',{}).get('chat_ids',[])))" 2>/dev/null || echo "")
fi

echo
echo "[3/4] Telegram bot token"
echo "  Open @BotFather on Telegram, run /newbot, follow prompts."
echo "  BotFather will give you a token like  1234567:AAAA-...."
if [ -n "${existing_token}" ]; then
    echo "  (a token is already configured; press Enter to keep it)"
fi
read -r -p "  Paste token (or Enter to keep existing): " token_input
token="${token_input:-${existing_token}}"
if [ -z "${token}" ]; then
    echo "no token supplied; aborting"
    exit 1
fi

# --- chat_id capture ----------------------------------------------------------
echo
echo "[4/4] chat_id capture"
echo "  On Telegram, find your bot and send it /start (any message works)."
echo "  Polling getUpdates for up to 120s..."

chat_id=""
end_ts=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "${end_ts}" ]; do
    resp=$(curl -fsS "https://api.telegram.org/bot${token}/getUpdates?limit=10&timeout=5" 2>/dev/null || echo "")
    chat_id=$(echo "${resp}" | "${VENV_DIR}/bin/python" -c "
import json,sys
try:
    data = json.load(sys.stdin)
    ids = sorted({u['message']['chat']['id'] for u in data.get('result', []) if 'message' in u and 'chat' in u['message']})
    print(ids[-1] if ids else '')
except Exception:
    print('')
")
    if [ -n "${chat_id}" ]; then
        break
    fi
    sleep 2
done

if [ -z "${chat_id}" ]; then
    if [ -n "${existing_chats}" ]; then
        echo "  no new message detected; keeping existing chat_ids: ${existing_chats}"
        chat_ids_json="[$(echo "${existing_chats}" | sed 's/,/, /g')]"
    else
        echo "  no message captured. Send /start to your bot, then re-run this script."
        exit 1
    fi
else
    echo "  captured chat_id=${chat_id}"
    chat_ids_json="[${chat_id}]"
fi

# --- write config -------------------------------------------------------------
"${VENV_DIR}/bin/python" - "${CONFIG_PATH}" "${token}" "${chat_ids_json}" <<'PY'
import json, os, sys
path, token, chats_json = sys.argv[1:4]
chats = json.loads(chats_json)
existing = {}
if os.path.exists(path):
    try:
        existing = json.load(open(path))
    except Exception:
        existing = {}
existing.setdefault("telegram", {})
existing["telegram"]["bot_token"] = token
existing["telegram"]["chat_ids"] = chats
existing.setdefault("stale_heartbeat_sec", 600)
existing.setdefault("mute_until_epoch", 0)
with open(path, "w") as f:
    json.dump(existing, f, indent=2)
os.chmod(path, 0o600)
print(f"wrote {path} (chmod 600)")
PY

echo
echo "Setup complete."
echo
echo "Next steps:"
echo "  1. Test the daemon in foreground:"
echo "       ${VENV_DIR}/bin/python /Users/mukuls/dev/assistant/bin/assistant-comms.py"
echo "     Send 'ping' to your bot; expect a 'pong' reply."
echo
echo "  2. When happy, load the LaunchAgent so it runs on boot:"
echo "       launchctl load -w ~/Library/LaunchAgents/com.assistant.assistant-comms.plist"
echo
echo "  Logs: ${LOG_DIR}/assistant-comms.launchd.{out,err}"
