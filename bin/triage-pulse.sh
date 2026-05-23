#!/bin/bash
# triage-pulse.sh — wake the Triage agent every 2 minutes via inbox + heartbeat.
#
# Cron-driven (every 120s via com.mukuls.triage-pulse LaunchAgent).
#
# Architecture:
#   1. Drop one pulse file in ~/.assistant/inbox/ (Triage drains these on each pulse).
#   2. Read ~/.assistant/heartbeat.json for Triage's CURRENT ws_ref. Triage updates
#      this file at the end of each of its own pulses, so it always reflects the
#      live workspace — no hand-maintained registry, no manual edits when Triage
#      respawns into a new workspace number.
#   3. Send one Enter keypress to that workspace to wake the conversation. Triage's
#      prompt routine then drains the inbox and runs the pulse logic.
#   4. If heartbeat is missing or stale (>10 min), call spawn-triage.sh to bring
#      up a fresh Triage workspace (auto-recovery from crash / cmux death / accidental
#      close).
#
# What this script does NOT do:
#   - It does NOT type "pulse-now" into the conversation. Just an Enter. The inbox
#     file is the actual signal; the Enter is just to nudge Claude out of idle.
#   - It does NOT verify the inbox was drained. That's Triage's job (it logs to
#     heartbeat.json with `pulses_drained_this_run`).

set -u
INBOX="$HOME/.assistant/inbox"
HEARTBEAT="$HOME/.assistant/heartbeat.json"
LEDGER_DIR="$HOME/.assistant/ledger"
LOG="$HOME/.assistant/triage-pulse.log"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
SPAWN_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spawn-triage.sh"
HEARTBEAT_STALE_SEC="${HEARTBEAT_STALE_SEC:-600}"   # 10 minutes

mkdir -p "$INBOX" "$LEDGER_DIR" "$(dirname "$LOG")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

# --- 1. Drop pulse file ----------------------------------------------------
TS=$(date +%s)
ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PULSE_FILE="$INBOX/pulse-$TS.json"
printf '{"ts":"%s","unix_ts":%d}\n' "$ISO" "$TS" > "$PULSE_FILE"

# Bound inbox growth: drop pulse files older than 1 hour. Triage normally
# drains them every 2 min; if it's been down longer, no point keeping them.
find "$INBOX" -maxdepth 1 -name 'pulse-*.json' -mmin +60 -delete 2>/dev/null

# --- 2. Read heartbeat ------------------------------------------------------
if [ ! -f "$HEARTBEAT" ]; then
    log "no heartbeat at $HEARTBEAT — running spawn-triage.sh"
    if [ -x "$SPAWN_SCRIPT" ]; then
        "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-triage exited non-zero"
    else
        log "spawn-triage.sh missing or non-exec at $SPAWN_SCRIPT"
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
    log "heartbeat has empty ws_ref — running spawn-triage.sh"
    [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-triage missing"
    exit 0
fi

if [ -n "$LAST_TS" ] && [ "$LAST_TS" -gt 0 ]; then
    AGE=$((NOW - LAST_TS))
    if [ "$AGE" -gt "$HEARTBEAT_STALE_SEC" ]; then
        log "heartbeat stale by ${AGE}s (>${HEARTBEAT_STALE_SEC}s) — running spawn-triage.sh"
        [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-triage missing"
        # Continue and still try to pulse the OLD ws_ref — if the spawn replaced
        # it, the new heartbeat will be in place by next tick.
    fi
fi

# --- 4. Verify the workspace still exists AND is actually Triage -----------
# After a cmux restart, workspace refs get reissued — workspace:12 may now be
# a completely different workspace. Tree-existence alone is not enough; we
# must confirm the title is still "Triage Agent ...". Otherwise we'll pulse
# an unrelated workspace (e.g. type "inbox" into a LinkedIn editing session).
if ! "$CMUX_BIN" tree --workspace "$WS" --json >/dev/null 2>&1; then
    log "workspace $WS no longer exists in cmux — running spawn-triage.sh"
    [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-triage missing"
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
    *"Triage Agent"*)
        ;;
    *)
        log "workspace $WS title is '$WS_TITLE' (expected to contain 'Triage Agent') — refs were reissued; running spawn-triage.sh"
        [ -x "$SPAWN_SCRIPT" ] && "$SPAWN_SCRIPT" 2>>"$LOG" || log "spawn-triage missing"
        exit 0
        ;;
esac

# --- 5. Wake Triage with a short literal text + Enter ---------------------
# The inbox file is the real signal — but a bare Enter alone is too weak; it
# can be interpreted as "continue what you were doing" if Triage is mid-thought
# rather than "there's new inbox to drain." Sending a short user message
# "inbox" gives Claude an explicit turn that maps to Step 0 of the routine.
# (The legacy `pulse-now` text is gone — the inbox file is the actual signal,
# and the text just needs to be a recognizable wake-word for log-grep'ing.)
"$CMUX_BIN" send --workspace "$WS" "inbox" >/dev/null 2>&1
"$CMUX_BIN" send-key --workspace "$WS" Return >/dev/null 2>&1
log "pulsed $WS (pulse_file=$PULSE_FILE)"
