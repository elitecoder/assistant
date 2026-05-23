#!/bin/bash
# triage-audit.sh — independent watchdog that catches Triage's stale awaiting cards.
#
# Reads ~/.claude/cache/triage-state.json, cross-checks each awaiting_input card
# against current ground truth (assistant-todo.json, gh pr view, cmux workspace
# list). If any card's predicate has been invalidated since the pulse that
# emitted it, log a warning and (optionally) nudge Triage with a corrective
# message naming the stale cards.
#
# Run by triage-pulse.sh ~30s after each pulse, or as its own cron. Idempotent.
#
# What this catches:
#   - autodispatch-unset:bulk cards listing TODOs whose autoDispatch is now
#     true (the 2026-05-23 incident).
#   - cleanup-gated cards for PRs that have since been MERGED.
#   - needs-you cards for workspaces that no longer exist.
#
# What it does NOT do:
#   - Mutate triage-state.json. Only Triage writes that file. We only nudge.
#   - Replace Triage's Step 2.5 re-validation. That's the primary defense; this
#     is a safety net for when Triage's prompt fails to enforce it.

set -u
STATE="$HOME/.claude/cache/triage-state.json"
TODO="$HOME/.claude/assistant-todo.json"
HEARTBEAT="$HOME/.assistant/heartbeat.json"
LOG="$HOME/.assistant/triage-audit.log"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
NUDGE="${TRIAGE_AUDIT_NUDGE:-1}"   # 0 to log only, 1 to send corrective nudge

mkdir -p "$(dirname "$LOG")"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

[ -f "$STATE" ] || { log "no triage-state.json yet"; exit 0; }
[ -f "$TODO" ] || { log "no assistant-todo.json"; exit 0; }

STALE_REPORT=$(python3 << 'PY'
import json, os, subprocess

state = json.load(open(os.path.expanduser("~/.claude/cache/triage-state.json")))
todo = json.load(open(os.path.expanduser("~/.claude/assistant-todo.json")))

# Build a quick lookup of current TODO autoDispatch state.
todo_auto = {}
for it in todo.get("items", []):
    todo_auto[it.get("id")] = it.get("autoDispatch")

stale = []

for card in state.get("awaiting_input", []) or []:
    key = card.get("key", "") or ""
    detail = card.get("detail", "") or ""

    # autodispatch-unset:* — predicate: at least one referenced TODO still has
    # autoDispatch=null. Extract td-NNN ids from detail; check the file.
    if "autodispatch-unset" in key:
        import re
        ids = re.findall(r"td-\d{3,4}", detail)
        if not ids:
            # Bulk card with no parsable IDs — fall through; can't validate.
            continue
        any_unset = any(todo_auto.get(tid) is None for tid in ids)
        if not any_unset:
            stale.append({
                "key": key,
                "reason": f"all referenced TODOs ({', '.join(ids)}) now have autoDispatch set; this card is stale",
            })

    # cleanup-gated:*:pr-NNN — predicate: PR is still OPEN.
    elif "cleanup-gated" in key:
        m = __import__("re").search(r"pr-(\d+)", key)
        if m:
            pr = m.group(1)
            try:
                out = subprocess.check_output(
                    ["gh", "pr", "view", pr, "--json", "state", "-q", ".state"],
                    text=True, stderr=subprocess.DEVNULL, timeout=10,
                ).strip()
                if out in ("MERGED", "CLOSED"):
                    stale.append({"key": key, "reason": f"PR #{pr} is now {out}; cleanup-gated card is stale"})
            except Exception:
                pass  # gh failure — don't flag stale; could be auth blip

print(json.dumps(stale))
PY
)

STALE_COUNT=$(printf '%s' "$STALE_REPORT" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
[ "$STALE_COUNT" = "0" ] && exit 0

log "found $STALE_COUNT stale awaiting card(s):"
printf '%s' "$STALE_REPORT" | python3 -c "
import json, sys
for s in json.load(sys.stdin):
    print(f'  - {s[\"key\"]}: {s[\"reason\"]}')
" >> "$LOG"

if [ "$NUDGE" = "0" ]; then
    log "TRIAGE_AUDIT_NUDGE=0 — log only, not nudging"
    exit 0
fi

# Find Triage's workspace from heartbeat
[ -f "$HEARTBEAT" ] || { log "no heartbeat — can't nudge"; exit 0; }
WS=$(python3 -c "
import json
try:
    print(json.load(open('$HEARTBEAT')).get('ws_ref','') or '')
except Exception:
    print('')
")
[ -z "$WS" ] && { log "heartbeat has no ws_ref"; exit 0; }

# Compose a one-line corrective nudge naming the stale keys.
KEYS_LIST=$(printf '%s' "$STALE_REPORT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(', '.join(s['key'] for s in data))
")
NUDGE_MSG="audit: stale awaiting cards detected (${KEYS_LIST}). Re-read state per Step 2.5 — these cards' predicates are no longer true. Drop them and dispatch any newly-eligible TODOs."

"$CMUX_BIN" send --workspace "$WS" "$NUDGE_MSG" >/dev/null 2>&1
"$CMUX_BIN" send-key --workspace "$WS" Return >/dev/null 2>&1
log "nudged $WS with stale-card list: $KEYS_LIST"
