#!/bin/bash
# spawn-comms.sh — bring up the assistant-comms Claude session in Terminal.app.
#
# Idempotent: safe to re-run. Used both for cold-start (LaunchAgent at boot)
# and recovery (assistant-pulse.sh calls this when comms heartbeat is stale).
#
# Strategy:
#   1. If a Terminal tab whose ID is recorded in ~/.assistant/comms/terminal-tab.txt
#      still belongs to a window AND that tab's last command was claude → exit 0.
#   2. Otherwise open a new Terminal window, run claude with the comms boot
#      prompt, and record the new tab's id for future pulse-driven wake-ups.
#
# Why Terminal.app and not cmux: Mukul wants the comms agent visible as a
# native Mac terminal so he can supervise it without launching cmux. Native
# Terminal also survives independently of cmux's lifecycle.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMS_DIR="$HOME/.assistant/comms"
HEARTBEAT="$COMMS_DIR/heartbeat.json"
LOG="$COMMS_DIR/spawn-comms.log"
TAB_FILE="$COMMS_DIR/terminal-tab.txt"
COMMS_PROMPT="$REPO_ROOT/prompts/prompt-assistant-comms-agent.md"
CWD="$HOME/dev/assistant"
TITLE="assistant-comms"

mkdir -p "$COMMS_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG" >&2; }

# Serialize concurrent spawns. mkdir is atomic on POSIX.
LOCK_DIR="$COMMS_DIR/spawn-comms.lock"
if [ -d "$LOCK_DIR" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
    if [ "$LOCK_AGE" -gt 300 ]; then
        log "stale lockdir (${LOCK_AGE}s) — removing"
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "another spawn-comms is running — exiting"
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# --- 1. Already alive? ------------------------------------------------------
# A live comms tab = (heartbeat fresh < 5min) AND (recorded tab id still
# exists in Terminal.app). Either condition failing → respawn.
if [ -f "$HEARTBEAT" ] && [ -f "$TAB_FILE" ]; then
    LAST_TS=$(python3 -c "
import json
try:
    print(json.load(open('$HEARTBEAT')).get('epoch') or 0)
except Exception:
    print(0)
")
    NOW=$(date +%s)
    AGE=$((NOW - LAST_TS))
    if [ "$LAST_TS" -gt 0 ] && [ "$AGE" -lt 300 ]; then
        TAB_ID=$(cat "$TAB_FILE" 2>/dev/null || echo "")
        if [ -n "$TAB_ID" ]; then
            ALIVE=$(osascript -e "
tell application \"Terminal\"
    set found to false
    repeat with w in windows
        repeat with t in tabs of w
            if (id of t as string) is \"$TAB_ID\" then
                set found to true
                exit repeat
            end if
        end repeat
        if found then exit repeat
    end repeat
    return found
end tell
" 2>/dev/null || echo "false")
            if [ "$ALIVE" = "true" ]; then
                log "comms tab id=$TAB_ID alive (heartbeat age ${AGE}s) — exit"
                exit 0
            fi
            log "tab id=$TAB_ID gone from Terminal — respawning"
        fi
    else
        log "heartbeat stale (${AGE}s) or missing — respawning"
    fi
fi

# --- 2. Sanity: comms config + venv exist ----------------------------------
if [ ! -f "$COMMS_DIR/config.json" ]; then
    log "missing $COMMS_DIR/config.json — run assistant-comms-setup.sh first"
    exit 1
fi
if [ ! -f "$COMMS_PROMPT" ]; then
    log "missing boot prompt at $COMMS_PROMPT"
    exit 1
fi

# --- 3. Resolve model + claude command -------------------------------------
# Source ~/.zprofile so we pick up CLAUDE_CODE_USE_BEDROCK if present (same
# pattern as spawn-assistant.sh — launchd does NOT source it for us).
[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1 || true

MODEL_SLUG="claude-sonnet-4-6[1m]"
if [ "${CLAUDE_CODE_USE_BEDROCK:-}" = "1" ]; then
    MODEL_ID="us.anthropic.$MODEL_SLUG"
else
    MODEL_ID="$MODEL_SLUG"
fi
log "model_id=$MODEL_ID use_bedrock=${CLAUDE_CODE_USE_BEDROCK:-0}"

# claude flags:
#   --dangerously-skip-permissions: comms reads/writes only its own dir +
#     calls a few CLIs. Permission prompts in a watched terminal would block
#     pulses indefinitely.
#   --add-dir: limits where it can read/write.
CLAUDE_CMD="cd $(printf %q "$CWD") && claude --dangerously-skip-permissions --add-dir ~/dev --add-dir ~/.assistant --add-dir ~/.claude --add-dir /tmp --model $(printf %q "$MODEL_ID")"

INSTRUCTION="Read $COMMS_PROMPT in full and execute every instruction in it. This is a fresh assistant-comms spawn — your first action should be to write your initial heartbeat to ~/.assistant/comms/heartbeat.json so the pulse script can find you."

# --- 4. Open a Terminal window, run claude, capture tab id ------------------
# Strategy: send the launch shell command via `do script` (returns the tab),
# wait for claude's banner (so the prompt is recognised), then send the
# Read-prompt instruction as user input followed by Enter.
TAB_ID=$(osascript <<APPLESCRIPT 2>>"$LOG"
tell application "Terminal"
    activate
    set newTab to do script "printf '\\033]0;${TITLE}\\007'; ${CLAUDE_CMD}"
    set tabId to id of newTab as string
    set custom title of newTab to "${TITLE}"
    return tabId
end tell
APPLESCRIPT
)

if [ -z "$TAB_ID" ]; then
    log "osascript failed to open Terminal tab"
    exit 1
fi
log "opened Terminal tab id=$TAB_ID"
echo "$TAB_ID" > "$TAB_FILE"

# --- 5. Wait for claude banner ---------------------------------------------
# Terminal.app exposes the visible buffer via `contents of selected tab`.
# Poll for "Claude Code v" or the bare prompt before sending the instruction.
CLAUDE_READY=0
for i in $(seq 1 30); do
    BUF=$(osascript -e "
tell application \"Terminal\"
    repeat with w in windows
        repeat with t in tabs of w
            if (id of t as string) is \"$TAB_ID\" then
                return contents of t
            end if
        end repeat
    end repeat
end tell
" 2>/dev/null || echo "")
    if printf '%s' "$BUF" | grep -qE 'Claude Code v|>\s*$|│\s*>'; then
        CLAUDE_READY=1
        break
    fi
    sleep 1
done

if [ "$CLAUDE_READY" != "1" ]; then
    log "claude never showed banner in 30s in tab $TAB_ID — leaving for diagnosis"
    exit 1
fi
log "claude ready in tab $TAB_ID"

# --- 6. Answer trust prompt if shown ---------------------------------------
if printf '%s' "$BUF" | grep -q '1\. Yes, I trust this folder'; then
    log "answering trust prompt"
    osascript -e "
tell application \"Terminal\"
    repeat with w in windows
        repeat with t in tabs of w
            if (id of t as string) is \"$TAB_ID\" then
                do script \"1\" in t
                exit repeat
            end if
        end repeat
    end repeat
end tell
" 2>>"$LOG" || true
    sleep 2
fi

# --- 7. Deliver the boot instruction ---------------------------------------
# `do script "<text>" in t` types the text into the existing tab's shell and
# presses Return. claude treats it as a user message.
osascript -e "
tell application \"Terminal\"
    repeat with w in windows
        repeat with t in tabs of w
            if (id of t as string) is \"$TAB_ID\" then
                do script $(printf %q "$INSTRUCTION") in t
                exit repeat
            end if
        end repeat
    end repeat
end tell
" 2>>"$LOG" || log "warn: failed to deliver boot instruction (claude may still be loading)"
log "delivered boot instruction to tab $TAB_ID"

# --- 8. Provisional heartbeat ----------------------------------------------
# Comms session will overwrite this on its first pulse. If something goes
# wrong before then, at least pulse-script knows where we are.
python3 - "$TAB_ID" <<'PY'
import datetime, json, os, sys
tab_id = sys.argv[1]
hb = {
    "ts": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "epoch": int(datetime.datetime.now(datetime.UTC).timestamp()),
    "pid": 0,
    "status": "spawn-bootstrap",
    "pulse_idx": 0,
    "terminal_tab_id": tab_id,
    "_note": "Provisional heartbeat written by spawn-comms.sh. Comms will overwrite on first pulse.",
}
path = os.path.expanduser("~/.assistant/comms/heartbeat.json")
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(hb, f, indent=2)
os.replace(tmp, path)
PY

log "spawn-comms complete — tab=$TAB_ID"
exit 0
