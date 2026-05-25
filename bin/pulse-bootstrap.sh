#!/bin/bash
# pulse-bootstrap.sh — pulse boot mechanics (no decisions).
#
# Does the ROUTINE work at the start of every pulse:
#   1. Identify the caller workspace + surface (writes to env-style stdout).
#   2. Drain the inbox (deletes pulse-*.json after counting).
#   3. Compute next pulse index.
#
# All decisions (skip pulse? respawn? dispatch?) stay in the Assistant prompt.
# This script just returns the facts the prompt needs.
#
# Usage:
#   eval "$(~/.claude/bin/pulse-bootstrap.sh)"
#   # exports: MY_WS, MY_SURFACE, PULSE_COUNT, LATEST_PULSE_TS,
#   #          PRIOR_PULSE_IDX, NEXT_PULSE_IDX

set -u

CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
INBOX="$HOME/.assistant/inbox"
STATE_PATH="$HOME/.claude/cache/assistant-state.json"

# 1. caller ws + surface (NEVER focused — that's whatever tab Mukul is on)
ID_JSON=$("$CMUX_BIN" identify --json 2>/dev/null || echo '{}')
read -r MY_WS MY_SURFACE <<<"$(printf '%s' "$ID_JSON" | python3 -c '
import json, sys, os
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
caller = d.get("caller", {})
ws = caller.get("workspace_ref") or os.environ.get("CMUX_WORKSPACE_ID", "")
surface = caller.get("pane_surface_ref") or caller.get("surface_ref") or ""
print(ws, surface)
')"

# 2. drain inbox
mkdir -p "$INBOX"
PULSE_COUNT=$(find "$INBOX" -maxdepth 1 -name 'pulse-*.json' 2>/dev/null | wc -l | tr -d ' ')
LATEST=$(find "$INBOX" -maxdepth 1 -name 'pulse-*.json' -print 2>/dev/null | sort | tail -n1)
LATEST_PULSE_TS=""
if [ -n "$LATEST" ]; then
    LATEST_PULSE_TS=$(python3 -c "
import json, sys
try: print(json.load(open('$LATEST')).get('ts',''))
except Exception: print('')
" 2>/dev/null || echo "")
fi
find "$INBOX" -maxdepth 1 -name 'pulse-*.json' -delete 2>/dev/null || true

# 3. pulse_idx
PRIOR_PULSE_IDX=$(python3 -c "
import json, os
p = os.path.expanduser('$STATE_PATH')
try: print(json.load(open(p)).get('_meta',{}).get('pulse_idx', 0))
except Exception: print(0)
" 2>/dev/null)
NEXT_PULSE_IDX=$((PRIOR_PULSE_IDX + 1))

# emit env-var assignments for eval
printf 'MY_WS=%q\n' "$MY_WS"
printf 'MY_SURFACE=%q\n' "$MY_SURFACE"
printf 'PULSE_COUNT=%q\n' "$PULSE_COUNT"
printf 'LATEST_PULSE_TS=%q\n' "$LATEST_PULSE_TS"
printf 'PRIOR_PULSE_IDX=%q\n' "$PRIOR_PULSE_IDX"
printf 'NEXT_PULSE_IDX=%q\n' "$NEXT_PULSE_IDX"
printf 'export MY_WS MY_SURFACE PULSE_COUNT LATEST_PULSE_TS PRIOR_PULSE_IDX NEXT_PULSE_IDX\n'
