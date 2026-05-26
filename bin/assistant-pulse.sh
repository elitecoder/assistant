#!/bin/bash
# assistant-pulse.sh — wake the Assistant agent every 2 minutes via inbox + heartbeat.
#
# Cron-driven (every 120s via com.assistant.assistant-pulse LaunchAgent).
#
# Architecture:
#   1. Drop one pulse file in ~/.assistant/inbox/ (Assistant drains these on each pulse).
#   2. Read ~/.assistant/heartbeat.json for Assistant's CURRENT ws_ref. Assistant updates
#      this file at the end of each of its own pulses, so it always reflects the
#      live workspace — no hand-maintained registry, no manual edits when Assistant
#      respawns into a new workspace number.
#   3. Send one Enter keypress to that workspace to wake the conversation. Assistant's
#      prompt routine then drains the inbox and runs the pulse logic.
#   4. If heartbeat is missing or stale (>10 min), call spawn-assistant.sh to bring
#      up a fresh Assistant workspace (auto-recovery from crash / cmux death / accidental
#      close).
#
# What this script does NOT do:
#   - It does NOT type "pulse-now" into the conversation. Just an Enter. The inbox
#     file is the actual signal; the Enter is just to nudge Claude out of idle.
#   - It does NOT verify the inbox was drained. That's Assistant's job (it logs to
#     heartbeat.json with `pulses_drained_this_run`).

set -u
INBOX="$HOME/.assistant/inbox"
HEARTBEAT="$HOME/.assistant/heartbeat.json"
LEDGER_DIR="$HOME/.assistant/ledger"
LOG="$HOME/.assistant/assistant-pulse.log"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
SPAWN_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spawn-assistant.sh"
HEARTBEAT_STALE_SEC="${HEARTBEAT_STALE_SEC:-600}"   # 10 minutes

mkdir -p "$INBOX" "$LEDGER_DIR" "$(dirname "$LOG")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

# Probe a cmux RPC up to 3 times with 2s backoff. Returns 0 on first success;
# only 3 consecutive failures count as a real "gone." Without this, a single
# transient `cmux tree` failure triggers a spurious respawn — exactly the bug
# that produced the 2026-05-23 zombie-workspace incident (ws:39, ws:41 spawned
# while ws:2 was still alive and healthy).
cmux_retry() {
    local i
    for i in 1 2 3; do
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
        [ "$i" -lt 3 ] && sleep 2
    done
    return 1
}

# --- 1. Drop pulse file ----------------------------------------------------
TS=$(date +%s)
ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PULSE_FILE="$INBOX/pulse-$TS.json"
printf '{"ts":"%s","unix_ts":%d}\n' "$ISO" "$TS" > "$PULSE_FILE"

# Bound inbox growth: drop pulse files older than 1 hour. Assistant normally
# drains them every 2 min; if it's been down longer, no point keeping them.
find "$INBOX" -maxdepth 1 -name 'pulse-*.json' -mmin +60 -delete 2>/dev/null

# --- 2. Read heartbeat ------------------------------------------------------
if [ ! -f "$HEARTBEAT" ]; then
    log "no heartbeat at $HEARTBEAT — running spawn-assistant.sh"
    if [ -x "$SPAWN_SCRIPT" ]; then
        "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-assistant exited non-zero"
    else
        log "spawn-assistant.sh missing or non-exec at $SPAWN_SCRIPT"
    fi
    exit 0
fi

WS=$(python3 -c "
import json, sys
try:
    print(json.load(open('$HEARTBEAT')).get('ws_ref','') or '')
except Exception:
    print('')
")
LAST_TS=$(python3 -c "
import json, sys
try:
    print(json.load(open('$HEARTBEAT')).get('last_pulse_ts','') or 0)
except Exception:
    print(0)
")

# --- 3. Stale-heartbeat check ----------------------------------------------
NOW=$(date +%s)
if [ -z "$WS" ]; then
    log "heartbeat has empty ws_ref — running spawn-assistant.sh"
    [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-assistant missing"
    exit 0
fi

if [ -n "$LAST_TS" ] && [ "$LAST_TS" -gt 0 ]; then
    AGE=$((NOW - LAST_TS))
    if [ "$AGE" -gt "$HEARTBEAT_STALE_SEC" ]; then
        # Before declaring stale, check if the Assistant's transcript is
        # actively being written. A long pulse (5-Agent fan-out + processing)
        # routinely takes 8-12 min, but Sonnet often forgets to re-write the
        # heartbeat between Step 0's initial write and Step N's final write.
        # If claude is still producing tool_use / tool_result records, the
        # session is alive — DON'T respawn; just bump the heartbeat ourselves.
        TRANSCRIPT_PATH=$(python3 -c "
import json, os
ws = '$WS'
try:
    w = json.load(open(os.path.expanduser('~/.claude/cache/world.json')))
    for s in w.get('live_sessions', []):
        if s.get('ws_ref') == ws:
            print(s.get('transcript_path', ''))
            break
except Exception:
    pass
" 2>/dev/null)

        TRANSCRIPT_AGE=99999
        if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
            TRANSCRIPT_MTIME=$(stat -f %m "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
            TRANSCRIPT_AGE=$((NOW - TRANSCRIPT_MTIME))
        fi

        # If transcript wrote anything in the last 120s, the Assistant is
        # mid-pulse. Bump heartbeat in-place; do NOT respawn.
        if [ "$TRANSCRIPT_AGE" -lt 120 ]; then
            log "heartbeat stale by ${AGE}s but transcript active (mtime ${TRANSCRIPT_AGE}s ago) — refreshing heartbeat in place"
            python3 -c "
import json, os, datetime
p = os.path.expanduser('~/.assistant/heartbeat.json')
try:
    hb = json.load(open(p))
except Exception:
    hb = {'ws_ref': '$WS', 'status': 'active', 'model': 'sonnet-4-6-1m'}
hb['last_pulse_ts'] = int(datetime.datetime.now(datetime.UTC).timestamp())
hb['last_pulse_iso'] = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
hb['_note'] = 'refreshed by assistant-pulse.sh (transcript active, Assistant mid-pulse)'
tmp = p + '.tmp'
with open(tmp, 'w') as f:
    json.dump(hb, f, indent=2)
os.replace(tmp, p)
" 2>/dev/null
        else
            log "heartbeat stale by ${AGE}s and transcript idle (mtime ${TRANSCRIPT_AGE}s ago) — running spawn-assistant.sh"
            [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-assistant missing"
            # Continue and still try to pulse the OLD ws_ref — if the spawn replaced
            # it, the new heartbeat will be in place by next tick.
        fi
    fi
fi

# --- 4. Verify the workspace still exists AND is actually Assistant -------
# After a cmux restart, workspace refs get reissued — workspace:12 may now be
# a completely different workspace. Tree-existence alone is not enough; we
# must confirm the title is still "Assistant (Sonnet 1M)". Otherwise we'll
# pulse an unrelated workspace (e.g. type "inbox" into a LinkedIn editing
# session). We accept legacy "Triage Agent" titles too so a pre-rename
# heartbeat doesn't get respawned away on the first tick after upgrade.
if ! cmux_retry "$CMUX_BIN" tree --workspace "$WS" --json; then
    log "workspace $WS not found after 3 retries — running spawn-assistant.sh"
    [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-assistant missing"
    exit 0
fi

WS_TITLE=$("$CMUX_BIN" list-workspaces 2>/dev/null | python3 -c "
import sys, re
target = sys.argv[1]
for line in sys.stdin:
    m = re.match(r'^\s*\*?\s*(workspace:\d+)\s+(.+?)(?:\s+\[selected\])?\s*$', line)
    if m and m.group(1) == target:
        print(m.group(2).strip())
        break
" "$WS")
case "$WS_TITLE" in
    *"Assistant (Sonnet 1M)"*|*"Triage Agent"*)
        ;;
    *)
        log "workspace $WS title is '$WS_TITLE' (expected to contain 'Assistant (Sonnet 1M)' or legacy 'Triage Agent') — refs were reissued; running spawn-assistant.sh"
        [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-assistant missing"
        exit 0
        ;;
esac

# --- 4b. Auto-/clear when transcript is large -----------------------------
# Sonnet 1M sessions degrade past ~50% context (~500K tokens, ~1.5MB JSONL).
# The pulse_idx>=150 self-respawn rule was a coarse proxy; the real signal
# is transcript size. When oversized, send /clear + prompt-reload to keep
# the same workspace + session_id alive but with a fresh context window.
# Cheaper than a full respawn (no spawn-assistant.sh, no zombie cleanup,
# no LaunchAgent target switch) and event-driven on the actual symptom.
#
# After /clear, reset pulse_idx in shared state (same reason as a respawn:
# the freshly-cleared Assistant will boot from prompt's Step 0 with no
# memory of its prior context).
CLEAR_THRESHOLD_BYTES="${CLEAR_THRESHOLD_BYTES:-1500000}"   # 1.5 MB
TRANSCRIPT_PATH=$(python3 -c "
import json, os
ws = '$WS'
try:
    w = json.load(open(os.path.expanduser('~/.claude/cache/world.json')))
    for s in w.get('live_sessions', []):
        if s.get('ws_ref') == ws:
            print(s.get('transcript_path', ''))
            break
except Exception:
    pass
" 2>/dev/null)

if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
    TRANSCRIPT_BYTES=$(stat -f %z "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
    if [ "$TRANSCRIPT_BYTES" -gt "$CLEAR_THRESHOLD_BYTES" ]; then
        log "transcript $TRANSCRIPT_BYTES bytes > $CLEAR_THRESHOLD_BYTES — sending /clear to $WS before pulse"

        # Reset pulse_idx so the cleared Assistant doesn't re-trip Step 0c
        python3 -c "
import json, os, datetime
p = os.path.expanduser('~/.claude/cache/assistant-state.json')
try:
    d = json.load(open(p))
except Exception:
    d = {}
prior = d.get('_meta', {}).get('pulse_idx', 0)
d.setdefault('_meta', {})['pulse_idx'] = 0
d['_meta']['pulse_idx_reset_at'] = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
d['_meta']['pulse_idx_reset_reason'] = 'assistant-pulse.sh: /clear (transcript $TRANSCRIPT_BYTES bytes)'
tmp = p + '.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.replace(tmp, p)
print(f'pulse_idx reset {prior} -> 0 (auto-clear)')
" 2>&1 | while read -r line; do log "$line"; done

        # /clear — Claude Code clears in-memory context, keeps session_id.
        # Send as a slash command (cmux send auto-Enters).
        "$CMUX_BIN" send --workspace "$WS" "/clear" >/dev/null 2>&1
        sleep 2

        # Re-deliver the prompt reference. The freshly-cleared Assistant has
        # no system message, no routine — needs the prompt to start over.
        REPO_ROOT_PROMPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/prompts/prompt-assistant-agent.md"
        "$CMUX_BIN" send --workspace "$WS" "Read $REPO_ROOT_PROMPT in full and execute every instruction in it. Context was just /cleared due to size; treat this as a fresh boot." >/dev/null 2>&1
        log "auto-cleared $WS — prompt re-delivered, skipping standard pulse this tick"
        exit 0
    fi
fi

# --- 5. Wake Assistant with a short literal text + Enter ------------------
# The inbox file is the real signal — but a bare Enter alone is too weak; it
# can be interpreted as "continue what you were doing" if Assistant is
# mid-thought rather than "there's new inbox to drain." Sending a short user
# message "inbox" gives Claude an explicit turn that maps to Step 0 of the
# routine. (The legacy `pulse-now` text is gone — the inbox file is the
# actual signal, and the text just needs to be a recognizable wake-word for
# log-grep'ing.)
"$CMUX_BIN" send --workspace "$WS" "inbox" >/dev/null 2>&1
"$CMUX_BIN" send-key --workspace "$WS" Return >/dev/null 2>&1
log "pulsed $WS (pulse_file=$PULSE_FILE)"

# --- 6. Schedule the post-pulse audit -------------------------------------
# After Assistant has had time to write its assistant-state.json (~90s
# typical), run the audit watchdog: catches stale awaiting cards Assistant's
# own Step 2.5 re-validation rule may have missed. Run in background so this
# script returns immediately to the LaunchAgent.
AUDIT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assistant-audit.sh"
if [ -x "$AUDIT_SCRIPT" ]; then
    (sleep 90 && "$AUDIT_SCRIPT" >/dev/null 2>&1) &
    disown 2>/dev/null || true
    log "scheduled post-pulse audit in 90s"
fi
