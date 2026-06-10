#!/usr/bin/env bash
# migrate-logs-to-assistant.sh — relocate Assistant LaunchAgent logs from the
# orchestrator's home (~/.architect/orchestrator-logs/) to the Assistant's own
# home (~/.assistant/logs/).
#
# WHY: ~/.architect is a DIFFERENT system's home (the architect/orchestrator).
# Assistant launchd captures and the two watcher app-logs historically landed
# there by accident. They belong under ~/.assistant/logs/. The plists and the
# two scripts have already been repointed in the repo; this script migrates the
# files that the OLD paths left behind on a running machine.
#
# SAFE BY DESIGN:
#   - Default run is a DRY RUN. It prints the plan and changes nothing.
#     Pass --apply to actually move files.
#   - Only Assistant-owned files are touched. ~/.architect/orchestrator-logs/
#     also holds NON-Assistant orchestrator logs (work-dispatcher.*,
#     hermes-executor.*, world-evaluator.*, …) — those are enumerated OUT, never
#     moved. The Assistant-owned set is an explicit allow-list (see ASSIST_LOGS).
#   - Idempotent: a file already moved (source gone) is skipped; a missing
#     source dir is a no-op; re-running after a successful --apply does nothing.
#   - Never clobbers: if a destination file already exists, the source is moved
#     aside to "<dest>.from-architect-<ts>" instead of overwriting it.
#   - Every move is appended to ~/.assistant/logs/migrate-logs-to-assistant.log
#     so it is reversible by hand.
#   - NEVER runs launchctl. Per the global rule (~/.claude/CLAUDE.md), this
#     script only PRINTS the exact bootout+bootstrap commands for each currently
#     loaded Assistant agent; the user runs them by hand to pick up the new
#     StandardOutPath/StandardErrorPath. (--apply gates the file moves ONLY.)
#
# USAGE:
#   bin/migrate-logs-to-assistant.sh              # dry-run: print the plan
#   bin/migrate-logs-to-assistant.sh --apply      # move files (still no launchctl)
#   bin/migrate-logs-to-assistant.sh --no-launchctl-hint   # suppress the manual
#                                                          # launchctl block
#                                                          # (used by install.sh,
#                                                          # which reloads agents
#                                                          # itself on --apply)

set -euo pipefail

HOME_DIR="${HOME}"
OLD_DIR="${HOME_DIR}/.architect/orchestrator-logs"
NEW_DIR="${HOME_DIR}/.assistant/logs"
MIGRATE_LOG="${NEW_DIR}/migrate-logs-to-assistant.log"

APPLY=0
LAUNCHCTL_HINT=1
TS="$(date +%Y%m%d-%H%M%S)"

log()  { printf '%s\n' "$*"; }
note() { printf '  · %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*" >&2; }

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --no-launchctl-hint) LAUNCHCTL_HINT=0 ;;
        -h|--help)
            sed -n '2,46p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) warn "unknown arg: $arg"; exit 2 ;;
    esac
done

# --- Assistant-owned log files (EXPLICIT allow-list) ------------------------
# launchd stdout/stderr captures for every Assistant LaunchAgent (stem →
# "<stem>.launchd.{out,err}") PLUS the two watchers' in-code app logs. Anything
# in OLD_DIR not named here is left untouched (it belongs to the orchestrator).
ASSIST_AGENT_STEMS=(
    assistant-pulse
    assistant-page
    session-context-watcher
    workspace-watcher
    slack-reactor
    world-scanner
    assistant-daemon
)

ASSIST_LOGS=()
for stem in "${ASSIST_AGENT_STEMS[@]}"; do
    ASSIST_LOGS+=("${stem}.launchd.out" "${stem}.launchd.err")
done
# In-code app logs written by the two repointed scripts:
#   world-scanner.py        → world-scanner.out
#   session-context-watcher → session-context-watcher.{out,err}
ASSIST_LOGS+=(
    world-scanner.out
    session-context-watcher.out
    session-context-watcher.err
)

# Map each agent stem → its launchd label (for the launchctl hint).
agent_label() {
    case "$1" in
        assistant-daemon) echo "com.mukul.assistant-daemon" ;;
        *)                echo "com.assistant.$1" ;;
    esac
}

# --- preamble ---------------------------------------------------------------
if [[ $APPLY -eq 0 ]]; then
    log "📋 DRY RUN — no files will be moved. Re-run with --apply to migrate."
else
    log "🔧 MIGRATING Assistant logs → ${NEW_DIR}"
fi
log "   from: ${OLD_DIR}"
log "   to:   ${NEW_DIR}"
log ""

# --- 1. ensure destination dir ----------------------------------------------
if [[ -d "$NEW_DIR" ]]; then
    note "OK   ${NEW_DIR} exists"
else
    note "MKDIR ${NEW_DIR}"
    if [[ $APPLY -eq 1 ]]; then
        mkdir -p "$NEW_DIR"
    fi
fi
log ""

# --- 2. move Assistant-owned files ------------------------------------------
log "[files] Moving Assistant-owned logs"
moved=0
skipped=0
collided=0

if [[ ! -d "$OLD_DIR" ]]; then
    note "source dir ${OLD_DIR} does not exist — nothing to migrate (no-op)"
else
    for name in "${ASSIST_LOGS[@]}"; do
        src="${OLD_DIR}/${name}"
        dst="${NEW_DIR}/${name}"

        if [[ ! -e "$src" ]]; then
            # Already migrated (or never existed) — idempotent skip.
            skipped=$((skipped + 1))
            continue
        fi

        if [[ -e "$dst" ]]; then
            # No-clobber: preserve both. Park the source beside the dest.
            collide_dst="${dst}.from-architect-${TS}"
            collided=$((collided + 1))
            note "COLLIDE ${name}: dest exists → move source to $(basename "$collide_dst")"
            if [[ $APPLY -eq 1 ]]; then
                mv "$src" "$collide_dst"
                printf '%s\tMOVE-COLLIDE\t%s\t->\t%s\n' "$TS" "$src" "$collide_dst" >> "$MIGRATE_LOG"
            fi
        else
            note "MOVE ${name}"
            if [[ $APPLY -eq 1 ]]; then
                mv "$src" "$dst"
                printf '%s\tMOVE\t%s\t->\t%s\n' "$TS" "$src" "$dst" >> "$MIGRATE_LOG"
            fi
        fi
        moved=$((moved + 1))
    done
fi
log ""
note "summary: ${moved} to move, ${skipped} already-migrated/absent, ${collided} name collisions"
if [[ $APPLY -eq 1 && $moved -gt 0 ]]; then
    note "moves logged to ${MIGRATE_LOG} (reversible by hand)"
fi
log ""

# --- 3. launchctl reload hint (PRINTED ONLY — never executed) ---------------
# Per ~/.claude/CLAUDE.md: never run launchctl load/bootstrap/bootout on the
# user's behalf. We print the exact commands for each currently loaded
# Assistant agent so the running daemons pick up the new log paths.
if [[ $LAUNCHCTL_HINT -eq 1 ]]; then
    log "[launchctl] Reload commands for loaded Assistant agents (run these by hand):"
    # Snapshot the loaded-label list ONCE. Re-piping `launchctl list | grep -q`
    # per iteration is both slow and racy under `set -o pipefail` (grep -q closes
    # the pipe on first match, SIGPIPE-ing launchctl), so we grep an in-memory copy.
    loaded_labels="$(launchctl list 2>/dev/null | awk '{print $3}')"
    any_loaded=0
    for stem in "${ASSIST_AGENT_STEMS[@]}"; do
        label="$(agent_label "$stem")"
        if printf '%s\n' "$loaded_labels" | grep -qx "$label"; then
            any_loaded=1
            plist="${HOME_DIR}/Library/LaunchAgents/${label}.plist"
            log "    launchctl bootout gui/\$UID/${label} 2>/dev/null; launchctl bootstrap gui/\$UID ${plist}"
        fi
    done
    if [[ $any_loaded -eq 0 ]]; then
        note "no Assistant agents currently loaded — nothing to reload"
    else
        log ""
        note "(each agent recreates its log file at the new path on next fire)"
    fi
    log ""
fi

# --- summary ----------------------------------------------------------------
if [[ $APPLY -eq 0 ]]; then
    log "✅ Dry-run complete. Re-run with --apply to move the files."
else
    log "✅ Migration complete."
fi
