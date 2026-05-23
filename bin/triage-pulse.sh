#!/bin/bash
# triage-pulse.sh — fire 'pulse-now' at the Triage agent so it re-triages.
# Cron-driven (every 120s via com.mukuls.triage-pulse LaunchAgent).
# Reads workspace_ref from a sidecar registry file written by the Evaluator
# at spawn time so we survive workspace renumbering.

set -u
REGISTRY="$HOME/.architect/triage-registry.json"
LOG="$HOME/.architect/orchestrator-logs/triage-pulse.out"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
mkdir -p "$(dirname "$LOG")"

if [ ! -f "$REGISTRY" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] no registry at $REGISTRY — Triage not spawned" >> "$LOG"
  exit 0
fi

WS=$(python3 -c "import json; print(json.load(open('$REGISTRY')).get('workspace_ref',''))")
SURF=$(python3 -c "import json; print(json.load(open('$REGISTRY')).get('surface_ref',''))")

if [ -z "$WS" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] empty workspace_ref in registry" >> "$LOG"
  exit 0
fi

# Verify the workspace still exists in cmux tree.
if ! "$CMUX_BIN" tree --workspace "$WS" --json >/dev/null 2>&1; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $WS no longer exists — Triage was closed" >> "$LOG"
  exit 0
fi

# Send pulse-now then Enter.
"$CMUX_BIN" send --workspace "$WS" "pulse-now" >/dev/null 2>&1
"$CMUX_BIN" send-key --workspace "$WS" Return >/dev/null 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pulsed $WS" >> "$LOG"
