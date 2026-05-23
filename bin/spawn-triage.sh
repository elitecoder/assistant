#!/bin/bash
# spawn-triage.sh — bring up a fresh Triage agent workspace.
#
# Idempotent: safe to re-run. Used for cold-start AND auto-recovery (called by
# triage-pulse.sh when ~/.assistant/heartbeat.json is missing or stale).
#
# Strategy:
#   1. Check if a fresh, alive Triage already exists (heartbeat <5min old AND
#      its ws_ref is still in cmux). If so, exit 0 — no respawn needed.
#   2. Otherwise, create a new cmux workspace at ~/.architect (Triage's cwd),
#      launch claude with the Sonnet 1M model and bypassPermissions, and deliver
#      the Triage prompt by reference (per spawn-claude-workspace skill).
#   3. Triage's own prompt routine writes the initial heartbeat on first pulse.
#
# Constraints we honor (from the spawn-claude-workspace skill):
#   - --focus true, otherwise the workspace has no terminal surface.
#   - --command "claude ..." for atomic launch (no separate send_text race).
#   - Bedrock model ID with us.anthropic. prefix when CLAUDE_CODE_USE_BEDROCK=1.
#   - Resolve cwd via realpath before slugging (macOS /private symlinks).
#   - Answer the trust prompt if first-launch in this cwd.
#   - Never stream the prompt body — Read it by reference from the repo.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
HEARTBEAT="$HOME/.assistant/heartbeat.json"
LOG="$HOME/.assistant/spawn-triage.log"
TRIAGE_PROMPT="$REPO_ROOT/prompts/prompt-triage-agent.md"
CWD="$HOME/.architect"
TITLE="Triage Agent (Sonnet 1M)"

mkdir -p "$(dirname "$LOG")" "$CWD"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG" >&2; }

# --- 1. Already alive? ------------------------------------------------------
if [ -f "$HEARTBEAT" ]; then
    EXISTING_WS=$(python3 -c "
import json
try:
    print(json.load(open('$HEARTBEAT')).get('ws_ref','') or '')
except Exception:
    print('')
")
    EXISTING_TS=$(python3 -c "
import json
try:
    print(json.load(open('$HEARTBEAT')).get('last_pulse_ts','') or 0)
except Exception:
    print(0)
")
    NOW=$(date +%s)
    if [ -n "$EXISTING_WS" ] && [ "$EXISTING_TS" -gt 0 ]; then
        AGE=$((NOW - EXISTING_TS))
        if [ "$AGE" -lt 300 ]; then
            if "$CMUX_BIN" tree --workspace "$EXISTING_WS" --json >/dev/null 2>&1; then
                log "existing Triage at $EXISTING_WS is alive (heartbeat age ${AGE}s) — exit"
                exit 0
            fi
        fi
        log "existing heartbeat for $EXISTING_WS is stale (${AGE}s) or workspace gone — respawning"
    fi
fi

# --- 2. Sanity: cmux running? -----------------------------------------------
if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
    log "cmux is not running — cannot spawn Triage. Start /Applications/cmux.app and retry."
    exit 1
fi

# --- 3. Resolve cwd + capture origin focus ---------------------------------
CWD_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")
ORIGIN_CTX=$("$CMUX_BIN" identify --json 2>/dev/null || echo '{}')
ORIGIN_WS=$(printf '%s' "$ORIGIN_CTX" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("focused",{}).get("workspace_ref","") or "")' 2>/dev/null || echo "")
ORIGIN_WIN=$(printf '%s' "$ORIGIN_CTX" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("focused",{}).get("window_ref","") or "")' 2>/dev/null || echo "")

# --- 4. Compose the launch command -----------------------------------------
# Sonnet 1M for routine pulse-driven work. Bedrock prefix when applicable.
MODEL_SLUG="claude-sonnet-4-6[1m]"
if [ "${CLAUDE_CODE_USE_BEDROCK:-}" = "1" ]; then
    MODEL_ID="us.anthropic.$MODEL_SLUG"
else
    MODEL_ID="$MODEL_SLUG"
fi
CLAUDE_CMD="claude --dangerously-skip-permissions --add-dir ~/dev --add-dir ~/.claude --add-dir ~/.architect --add-dir ~/.assistant --add-dir /tmp --model \"$MODEL_ID\""

# --- 5. Create the workspace ------------------------------------------------
log "creating Triage workspace at cwd=$CWD_REAL"
OUT=$("$CMUX_BIN" new-workspace --cwd "$CWD_REAL" --name "$TITLE" --focus true --command "$CLAUDE_CMD" 2>&1)
WS_REF=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -n1)
if [ -z "$WS_REF" ]; then
    log "new-workspace failed: $OUT"
    exit 1
fi
log "new workspace = $WS_REF"

# Find the initial surface
sleep 1
SURFACE_REF=$("$CMUX_BIN" list-pane-surfaces --workspace "$WS_REF" 2>/dev/null | grep -oE 'surface:[0-9]+' | head -n1)
if [ -z "$SURFACE_REF" ]; then
    log "could not find surface in $WS_REF"
    exit 1
fi
log "surface = $SURFACE_REF"

# --- 6. Wait for claude readiness (and answer trust prompt if shown) -------
sleep 2
TRUST_TEXT=$("$CMUX_BIN" rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":40}))' "$SURFACE_REF")" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin); print(d.get("text",""))
except Exception:
    pass' 2>/dev/null || echo "")
if printf '%s' "$TRUST_TEXT" | grep -q '1\. Yes, I trust this folder'; then
    log "answering trust prompt"
    "$CMUX_BIN" rpc surface.send_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"text":"1"}))' "$SURFACE_REF")" >/dev/null
    "$CMUX_BIN" rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
fi

CLAUDE_READY=0
for i in $(seq 1 30); do
    TEXT=$("$CMUX_BIN" rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":40}))' "$SURFACE_REF")" 2>/dev/null \
        | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin); print(d.get("text",""))
except Exception:
    pass' 2>/dev/null || echo "")
    if printf '%s' "$TEXT" | grep -qE 'Claude Code v'; then
        CLAUDE_READY=1
        break
    fi
    sleep 1
done

if [ "$CLAUDE_READY" != "1" ]; then
    log "claude never showed banner in 30s in $WS_REF — leaving workspace up for diagnosis"
    exit 1
fi
log "claude ready in $WS_REF"

# --- 7. Deliver the Triage prompt by reference -----------------------------
INSTRUCTION="Read $TRIAGE_PROMPT in full and execute every instruction in it. This is a fresh Triage spawn — your first action should be to write your initial heartbeat to ~/.assistant/heartbeat.json so the pulse script can find you."
python3 - "$SURFACE_REF" "$INSTRUCTION" <<'PYEOF'
import json, subprocess, sys
surface, text = sys.argv[1], sys.argv[2]
subprocess.run(
    ["cmux", "rpc", "surface.send_text",
     json.dumps({"surface_id": surface, "text": text.rstrip("\n")})],
    check=True,
    capture_output=True,
)
PYEOF
sleep 1
"$CMUX_BIN" rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null

# --- 8. Restore origin focus -----------------------------------------------
if [ -n "$ORIGIN_WIN" ]; then
    "$CMUX_BIN" focus-window --window "$ORIGIN_WIN" >/dev/null 2>&1 || true
fi
if [ -n "$ORIGIN_WS" ]; then
    "$CMUX_BIN" select-workspace --workspace "$ORIGIN_WS" >/dev/null 2>&1 || true
fi

# --- 9. Write a provisional heartbeat so the next pulse finds the new ws --
# Triage will overwrite this with its own heartbeat on first pulse — but if
# something goes wrong before then, at least the next pulse-script tick can
# wake the new workspace.
python3 - "$WS_REF" "$SURFACE_REF" <<'PY'
import json, os, datetime
ws, surf = __import__("sys").argv[1], __import__("sys").argv[2]
hb = {
    "ws_ref": ws,
    "surface_ref": surf,
    "last_pulse_iso": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "last_pulse_ts": int(datetime.datetime.utcnow().timestamp()),
    "status": "spawn-bootstrap",
    "model": "sonnet-4-6-1m",
    "_note": "Provisional heartbeat written by spawn-triage.sh. Triage will overwrite on first pulse.",
}
path = os.path.expanduser("~/.assistant/heartbeat.json")
with open(path + ".tmp", "w") as f:
    json.dump(hb, f, indent=2)
os.replace(path + ".tmp", path)
PY

log "Triage spawned → $WS_REF $SURFACE_REF (heartbeat written)"
echo "workspace=$WS_REF surface=$SURFACE_REF claude_ready=$CLAUDE_READY"
