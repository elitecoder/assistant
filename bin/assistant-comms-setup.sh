#!/usr/bin/env bash
# assistant-comms-setup.sh — first-run bootstrap for assistant-comms.
#
# 1. Create the uv-managed venv at ~/.assistant/comms-venv/.
# 2. Install python-telegram-bot (Telegram path only).
# 3. Prompt for transport choice (Telegram or Discord), collect credentials.
# 4. Write ~/.assistant/comms/config.json (chmod 600).
# 5. Send a test message to validate credentials and connectivity.
# 6. Load the KeepAlive LaunchAgent so the daemon starts now and on every boot.
#
# Idempotent. Safe to re-run.

set -euo pipefail

HOME_DIR="${HOME}"
VENV_DIR="${HOME_DIR}/.assistant/comms-venv"
CONFIG_DIR="${HOME_DIR}/.assistant/comms"
CONFIG_PATH="${CONFIG_DIR}/config.json"
LOG_DIR="${HOME_DIR}/.architect/orchestrator-logs"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "${CONFIG_DIR}" "${LOG_DIR}"

# --- venv (always created; Telegram path needs python-telegram-bot) -----------
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

# --- transport choice ---------------------------------------------------------
existing_transport=""
if [ -f "${CONFIG_PATH}" ]; then
    existing_transport=$("${VENV_DIR}/bin/python" -c \
        "import json,sys; print(json.load(open('${CONFIG_PATH}')).get('transport',''))" \
        2>/dev/null || echo "")
fi

echo
echo "[2/4] Transport"
echo "  Which messaging platform should this machine use?"
echo "    1) Telegram  — bot token + chat_id auto-captured"
echo "    2) Discord   — bot token + DM channel ID (manual)"
if [ -n "${existing_transport}" ]; then
    echo "  (currently configured: ${existing_transport}; press Enter to keep)"
fi
read -r -p "  Choice [1/2, or Enter to keep existing]: " transport_input

if [ -z "${transport_input}" ] && [ -n "${existing_transport}" ]; then
    transport="${existing_transport}"
elif [ "${transport_input}" = "2" ]; then
    transport="discord"
else
    transport="telegram"
fi
echo "  Using transport: ${transport}"

# --- Telegram setup -----------------------------------------------------------
if [ "${transport}" = "telegram" ]; then
    echo
    echo "[3/4] installing python-telegram-bot into venv"
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "${VENV_DIR}/bin/python" 'python-telegram-bot>=21,<22'
    else
        "${VENV_DIR}/bin/pip" install --upgrade pip
        "${VENV_DIR}/bin/pip" install 'python-telegram-bot>=21,<22'
    fi

    existing_token=""
    existing_chats=""
    if [ -f "${CONFIG_PATH}" ]; then
        existing_token=$("${VENV_DIR}/bin/python" -c \
            "import json,sys; print(json.load(open('${CONFIG_PATH}')).get('telegram',{}).get('bot_token',''))" \
            2>/dev/null || echo "")
        existing_chats=$("${VENV_DIR}/bin/python" -c \
            "import json,sys; print(','.join(str(x) for x in json.load(open('${CONFIG_PATH}')).get('telegram',{}).get('chat_ids',[])))" \
            2>/dev/null || echo "")
    fi

    echo
    echo "  Open @BotFather on Telegram, run /newbot, follow prompts."
    echo "  BotFather will give you a token like  1234567:AAAA-...."
    if [ -n "${existing_token}" ]; then
        echo "  (a token is already configured; press Enter to keep it)"
    fi
    read -r -p "  Paste token (or Enter to keep existing): " token_input
    tg_token="${token_input:-${existing_token}}"
    if [ -z "${tg_token}" ]; then
        echo "no token supplied; aborting"
        exit 1
    fi

    echo
    echo "[4/4] chat_id capture"
    echo "  On Telegram, find your bot and send it /start (any message works)."
    echo "  Polling getUpdates for up to 120s..."

    chat_id=""
    end_ts=$(( $(date +%s) + 120 ))
    while [ "$(date +%s)" -lt "${end_ts}" ]; do
        resp=$(curl -fsS "https://api.telegram.org/bot${tg_token}/getUpdates?limit=10&timeout=5" \
            2>/dev/null || echo "")
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

    "${VENV_DIR}/bin/python" - "${CONFIG_PATH}" "${tg_token}" "${chat_ids_json}" <<'PY'
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
existing["transport"] = "telegram"
existing.setdefault("stale_heartbeat_sec", 600)
existing.setdefault("mute_until_epoch", 0)
with open(path, "w") as f:
    json.dump(existing, f, indent=2)
os.chmod(path, 0o600)
print(f"wrote {path} (chmod 600)")
PY

# --- Discord setup ------------------------------------------------------------
else
    echo
    echo "[3/4] Discord bot token"
    echo "  Before running this step, complete the one-time bot setup:"
    echo "    1. Go to https://discord.com/developers/applications → New Application → add a Bot"
    echo "    2. Bot tab → Privileged Gateway Intents → enable Message Content Intent → Save"
    echo "    3. OAuth2 → URL Generator → scope: bot → permissions: Send Messages,"
    echo "       Read Messages/View Channels, Read Message History → copy URL → invite bot to server"
    echo "  Then: Bot tab → Reset Token → copy the token."
    if [ -f "${CONFIG_PATH}" ]; then
        existing_dc_token=$("${VENV_DIR}/bin/python" -c \
            "import json,sys; print(json.load(open('${CONFIG_PATH}')).get('discord',{}).get('bot_token',''))" \
            2>/dev/null || echo "")
        if [ -n "${existing_dc_token}" ]; then
            echo "  (a token is already configured; press Enter to keep it)"
        fi
    else
        existing_dc_token=""
    fi
    read -r -p "  Paste bot token (or Enter to keep existing): " dc_token_input
    dc_token="${dc_token_input:-${existing_dc_token}}"
    if [ -z "${dc_token}" ]; then
        echo "no token supplied; aborting"
        exit 1
    fi

    echo
    echo "[4/4] Discord channel ID for this machine"
    echo "  Each machine gets its own dedicated channel in your server (e.g. #macbook-pro)."
    echo "  To get the channel ID:"
    echo "    1. Discord Settings → Advanced → enable Developer Mode"
    echo "    2. In your server, right-click the channel for this machine → Copy Channel ID"
    echo "  The bot must have Send Messages + Read Messages permissions in that channel."
    existing_dc_channel=""
    if [ -f "${CONFIG_PATH}" ]; then
        existing_dc_channel=$("${VENV_DIR}/bin/python" -c \
            "import json,sys; print(json.load(open('${CONFIG_PATH}')).get('discord',{}).get('channel_id',''))" \
            2>/dev/null || echo "")
        if [ -n "${existing_dc_channel}" ]; then
            echo "  (currently configured: ${existing_dc_channel}; press Enter to keep)"
        fi
    fi
    read -r -p "  Paste channel ID (or Enter to keep existing): " dc_channel_input
    dc_channel="${dc_channel_input:-${existing_dc_channel}}"
    if [ -z "${dc_channel}" ]; then
        echo "no channel ID supplied; aborting"
        exit 1
    fi

    "${VENV_DIR}/bin/python" - "${CONFIG_PATH}" "${dc_token}" "${dc_channel}" <<'PY'
import json, os, sys
path, token, channel_id = sys.argv[1:4]
existing = {}
if os.path.exists(path):
    try:
        existing = json.load(open(path))
    except Exception:
        existing = {}
existing.setdefault("discord", {})
existing["discord"]["bot_token"] = token
existing["discord"]["channel_id"] = int(channel_id)
existing["transport"] = "discord"
existing.setdefault("stale_heartbeat_sec", 600)
existing.setdefault("mute_until_epoch", 0)
with open(path, "w") as f:
    json.dump(existing, f, indent=2)
os.chmod(path, 0o600)
print(f"wrote {path} (chmod 600)")
PY
fi

echo
echo "[3/3] Validating — sending a test message to confirm connectivity..."

SEND_RC=1
if [ "${transport}" = "discord" ]; then
    CHANNEL_ID=$("${VENV_DIR}/bin/python" -c \
        "import json,sys; print(json.load(open('${CONFIG_PATH}')).get('discord',{}).get('channel_id',''))" \
        2>/dev/null || echo "")
    if [ -n "${CHANNEL_ID}" ]; then
        SEND_OUT=$("${VENV_DIR}/bin/python" "${REPO_DIR}/bin/discord-send.py" \
            --text "✅ assistant setup complete — this machine is connected." \
            --channel "${CHANNEL_ID}" --kind reply 2>&1)
        SEND_RC=$?
    fi
else
    SEND_OUT=$("${VENV_DIR}/bin/python" "${REPO_DIR}/bin/tg-send.py" \
        --text "✅ assistant setup complete — this machine is connected." \
        --kind reply 2>&1)
    SEND_RC=$?
fi

if [ "${SEND_RC}" -ne 0 ]; then
    echo "  ✗ Test message failed. Check your credentials and bot permissions."
    echo "  Output: ${SEND_OUT}"
    echo "  Fix the issue and re-run this script."
    exit 1
fi

echo "  ✓ Test message sent successfully."
echo

# --- Load the LaunchAgent so the daemon starts now and on every boot ----------
PLIST="${HOME_DIR}/Library/LaunchAgents/com.assistant.assistant-comms.plist"
if [ -f "${PLIST}" ]; then
    echo "[4/4] Loading LaunchAgent (daemon will start now and on every boot)..."
    launchctl bootout "gui/${UID}/com.assistant.assistant-comms" 2>/dev/null || true
    launchctl bootstrap "gui/${UID}" "${PLIST}" 2>/dev/null || launchctl load -w "${PLIST}"
    sleep 2
    if launchctl list | grep -q "com.assistant.assistant-comms"; then
        echo "  ✓ Daemon running (PID $(launchctl list | grep com.assistant.assistant-comms | awk '{print $1}'))."
    else
        echo "  ✗ Daemon did not start. Check logs at: ${LOG_DIR}/assistant-comms.launchd.{out,err}"
        exit 1
    fi
else
    echo "  ⚠ LaunchAgent plist not found at ${PLIST}."
    echo "  Run: ./install.sh --apply   to install it, then re-run this script."
    exit 1
fi

echo
echo "Setup complete. The assistant is running and connected."
echo "Logs: ${LOG_DIR}/assistant-comms.launchd.{out,err}"
