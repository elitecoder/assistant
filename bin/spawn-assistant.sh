#!/bin/bash
# spawn-assistant.sh — bring up a fresh Assistant agent workspace.
#
# Idempotent: safe to re-run. Used for cold-start AND auto-recovery (called by
# assistant-pulse.sh when ~/.assistant/heartbeat.json is missing or stale).
#
# Strategy:
#   1. Check if a fresh, alive Assistant already exists (heartbeat <5min old AND
#      its ws_ref is still in cmux). If so, exit 0 — no respawn needed.
#   2. Otherwise, create a new cmux workspace at ~/.architect (Assistant's cwd),
#      launch claude with the Sonnet 1M model and bypassPermissions, and deliver
#      the Assistant prompt by reference (per spawn-claude-workspace skill).
#   3. Assistant's own prompt routine writes the initial heartbeat on first pulse.
#
# Constraints we honor (from the spawn-claude-workspace skill):
#   - --focus false (default) — this script runs in the background (cron-fired
#     by the pulse watchdog when heartbeat goes stale), so it MUST NOT steal
#     Mukul's foreground tab. Re-verified 2026-05-23: current cmux creates a
#     fully-functional surface without focus, the old "zero panes" warning
#     is obsolete.
#   - --command "claude ..." for atomic launch (no separate send_text race).
#   - Bedrock model ID with us.anthropic. prefix when CLAUDE_CODE_USE_BEDROCK=1.
#   - Resolve cwd via realpath before slugging (macOS /private symlinks).
#   - Answer the trust prompt if first-launch in this cwd.
#   - Never stream the prompt body — Read it by reference from the repo.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
HEARTBEAT="$HOME/.assistant/heartbeat.json"
LOG="$HOME/.assistant/spawn-assistant.log"
ASSISTANT_PROMPT="$REPO_ROOT/prompts/prompt-assistant-agent.md"
CWD="$HOME/.architect"
TITLE="Assistant (Sonnet 1M)"

mkdir -p "$(dirname "$LOG")" "$CWD"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG" >&2; }

# Serialize concurrent spawns (e.g. cron pulse + manual respawn within ~2s).
# Without this, two parallel runs each pass the "alive?" check, both create
# workspaces, and the zombie cleanup at the end of each runs before the other
# is visible — leaving 2 Assistants alive. Reproduced 2026-05-23: ws:42+ws:43.
#
# `mkdir` is atomic on POSIX, so it's a portable mutex that works without flock.
# A stale lockdir from a previous crash gets cleaned up if it's >5min old.
LOCK_DIR="$HOME/.assistant/spawn-assistant.lock"
if [ -d "$LOCK_DIR" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
    if [ "$LOCK_AGE" -gt 300 ]; then
        log "stale lockdir ($LOCK_AGE s old) — removing"
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "another spawn-assistant is running (lockdir exists) — exiting"
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# Probe a cmux RPC up to 3 times with 2s backoff. Returns 0 if any attempt
# succeeds; only returns non-zero when all 3 fail. A single transient failure
# must NOT trigger a respawn — that's how we ended up spawning ws:39 then ws:41
# while the original ws:2 was still alive (2026-05-23 incident).
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
                # cmux restarts reissue workspace refs — confirm the title still
                # matches Assistant before declaring it alive. Otherwise we'd skip
                # respawn while the heartbeat points at someone else's workspace.
                EXISTING_TITLE=$("$CMUX_BIN" list-workspaces 2>/dev/null | python3 -c "
import sys, re
target = sys.argv[1]
for line in sys.stdin:
    m = re.match(r'^\s*\*?\s*(workspace:\d+)\s+(.+?)(?:\s+\[selected\])?\s*$', line)
    if m and m.group(1) == target:
        print(m.group(2).strip())
        break
" "$EXISTING_WS")
                case "$EXISTING_TITLE" in
                    *"Assistant (Sonnet 1M)"*)
                        log "existing Assistant at $EXISTING_WS is alive (heartbeat age ${AGE}s, title='$EXISTING_TITLE') — exit"
                        exit 0
                        ;;
                    *"Triage Agent"*)
                        log "heartbeat points at $EXISTING_WS (legacy 'Triage Agent' title from pre-rename) — respawning under new name"
                        ;;
                    *)
                        log "heartbeat points at $EXISTING_WS but its title is '$EXISTING_TITLE' (refs reissued) — respawning"
                        ;;
                esac
            fi
        fi
        log "existing heartbeat for $EXISTING_WS is stale (${AGE}s) or workspace gone — respawning"
    fi
fi

# --- 2. Sanity: cmux running? -----------------------------------------------
# Retry to absorb transient RPC failures — cmux is healthy but `ping` can
# briefly fail under load. Only treat 3 consecutive failures as "really down".
if ! cmux_retry "$CMUX_BIN" ping; then
    log "cmux ping failed 3× in a row — cannot spawn Assistant. Start /Applications/cmux.app and retry."
    exit 1
fi

# --- 3. Resolve cwd ---------------------------------
# We don't take/restore focus — `--focus false` on new-workspace below means
# we never disturb Mukul's foreground tab in the first place.
CWD_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")

# --- 4. Compose the launch command -----------------------------------------
# Sonnet 1M for routine pulse-driven work. Bedrock prefix when applicable.
#
# Backend env (CLAUDE_CODE_USE_BEDROCK, AWS_REGION, AWS_BEARER_TOKEN_BEDROCK)
# is exported by ~/.zprofile, which cmux-launched claude sources via its login
# shell. We don't need to re-export those here. We only need to know whether
# the spawned claude will use Bedrock so we pick a Bedrock-compatible model ID.
#
# Detection: launchd-fired bash does NOT source ~/.zprofile, so $CLAUDE_CODE_USE_BEDROCK
# is empty when this script runs from the LaunchAgent. Source ~/.zprofile (best-effort)
# to pick up the same exports the cmux-launched claude will see.
[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1 || true

MODEL_SLUG="claude-sonnet-4-6[1m]"
if [ "${CLAUDE_CODE_USE_BEDROCK:-}" = "1" ]; then
    MODEL_ID="us.anthropic.$MODEL_SLUG"
else
    MODEL_ID="$MODEL_SLUG"
fi
log "model_id=$MODEL_ID use_bedrock=${CLAUDE_CODE_USE_BEDROCK:-0}"
CLAUDE_CMD="claude --dangerously-skip-permissions --add-dir ~/dev --add-dir ~/.claude --add-dir ~/.architect --add-dir ~/.assistant --add-dir /tmp --model \"$MODEL_ID\""

# --- 5. Create the workspace ------------------------------------------------
log "creating Assistant workspace at cwd=$CWD_REAL"
OUT=$("$CMUX_BIN" new-workspace --cwd "$CWD_REAL" --name "$TITLE" --focus false --command "$CLAUDE_CMD" 2>&1)
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

# --- 7. Deliver the Assistant prompt by reference --------------------------
INSTRUCTION="Read $ASSISTANT_PROMPT in full and execute every instruction in it. This is a fresh Assistant spawn — your first action should be to write your initial heartbeat to ~/.assistant/heartbeat.json so the pulse script can find you."
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

# --- 8. (No focus restore needed — we spawned with --focus false, so we
#       never took Mukul's foreground in the first place.) ------------------

# --- 9. Write a provisional heartbeat so the next pulse finds the new ws --
# Assistant will overwrite this with its own heartbeat on first pulse — but if
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
    "_note": "Provisional heartbeat written by spawn-assistant.sh. Assistant will overwrite on first pulse.",
}
path = os.path.expanduser("~/.assistant/heartbeat.json")
with open(path + ".tmp", "w") as f:
    json.dump(hb, f, indent=2)
os.replace(path + ".tmp", path)
PY

# --- 10. Zombie cleanup: close any other "Assistant (Sonnet 1M)" workspaces -
# When a false-positive in the liveness check triggered a respawn, the prior
# Assistant workspace stayed alive in cmux even though we no longer pulse it.
# After we've successfully spawned the NEW Assistant, find every other ws with
# the same title and close it. Keep only the freshly spawned $WS_REF.
"$CMUX_BIN" list-workspaces 2>/dev/null | python3 - "$WS_REF" <<'PY' | while IFS= read -r zombie_ws; do
import sys, re
me = sys.argv[1]
for line in sys.stdin:
    m = re.match(r'^\s*\*?\s*(workspace:\d+)\s+(.+?)(?:\s+\[selected\])?\s*$', line)
    if not m:
        continue
    ws, title = m.group(1), m.group(2).strip()
    if ws == me:
        continue
    if "Assistant (Sonnet 1M)" in title or title.startswith("Triage Agent"):
        print(ws)
PY
    log "closing zombie Assistant workspace $zombie_ws"
    "$CMUX_BIN" close-workspace --workspace "$zombie_ws" >/dev/null 2>&1 \
        || log "failed to close $zombie_ws (may already be gone)"
done

log "Assistant spawned → $WS_REF $SURFACE_REF (heartbeat written)"
echo "workspace=$WS_REF surface=$SURFACE_REF claude_ready=$CLAUDE_READY"
