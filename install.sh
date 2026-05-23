#!/usr/bin/env bash
# install.sh — wire the live system at ~/.claude/ + ~/Library/LaunchAgents/
# to the canonical code in this repo at ~/dev/assistant/.
#
# Idempotent: re-run it freely. Default is --dry-run; pass --apply to mutate.
#
# Strategy:
#   - Code (bin/, prompts/, lessons/, skills/) is symlinked from ~/.claude/* to
#     this repo, so edits go live without copying.
#   - LaunchAgent plists are COPIED into ~/Library/LaunchAgents/ (launchd does
#     not follow symlinks reliably) and then unloaded + reloaded.
#   - Skills are symlinked per-name into ~/.claude/skills/<name> → repo's
#     skills/<name>/. Other skills under ~/.claude/skills/ are left untouched.
#   - Runtime state stays where it is (~/.claude/cache/, ~/.claude/projects/,
#     ~/.claude/assistant-todo.json, ~/.claude/assistant-dashboard.html). This
#     install never touches those.
#
# What gets backed up:
#   - Anything currently at the target path that is NOT already a symlink to
#     the repo's expected source is moved aside to <target>.bak-<unix-ts>.
#     You can `rm -rf` those once you're confident the install is good.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME}"
APPLY=0
PULL_SKILLS=0
TS="$(date +%s)"

log() { printf '%s\n' "$*"; }
note() { printf '  · %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*" >&2; }

# --- argv parsing -----------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --pull-skills) PULL_SKILLS=1 ;;
        -h|--help)
            cat <<USAGE
install.sh — install/update the Assistant system from $REPO_ROOT

  --dry-run         (default) Show what would change. No mutation.
  --apply           Actually create symlinks/copies, copy plists, reload launchd.
  --pull-skills     Pull edits from the live ~/.claude/skills/<name>/ directories
                    BACK into the repo (inverse of the install copy step).
                    Use this if you edited a skill in place and want to commit it.
                    Prints a unified diff in dry-run; run with --apply to copy.
  -h, --help        This help.

After --apply:
  - ~/.claude/bin                                → symlink → $REPO_ROOT/bin
  - ~/.claude/spawn-prompts/prompt-triage-agent.md → symlink → $REPO_ROOT/prompts/...
  - ~/.claude/lessons/active                     → symlink → $REPO_ROOT/lessons/active
  - ~/.claude/skills/{todo,cleanup,spawn-claude-workspace} → COPIES (shareable)
  - ~/Library/LaunchAgents/com.mukuls.{world-scanner,triage-pulse,assistant-page,
       session-context-watcher,assistant-todo-server}.plist → COPIED
  - launchd: kickstart -k each agent (load if not loaded)

Skills are copied (not symlinked) so they're standalone shareable artifacts.
The trade-off: in-place edits to ~/.claude/skills/<name>/ don't auto-sync to
the repo. install.sh detects drift on dry-run and warns. To bring live edits
into the repo, use --pull-skills.

What is NOT touched:
  - ~/.claude/cache/, ~/.claude/projects/, ~/.claude/cmux-registry.json
  - ~/.claude/assistant-todo.json, ~/.claude/assistant-dashboard.html
  - ~/.claude/skills/cmux/ and any other unrelated skills
  - ~/.architect/
USAGE
            exit 0 ;;
        *) warn "unknown arg: $arg"; exit 2 ;;
    esac
done

if [[ $PULL_SKILLS -eq 1 ]]; then
    log "↩️  PULL SKILLS mode — copying live ~/.claude/skills/<name>/ → repo"
    if [[ $APPLY -eq 0 ]]; then
        log "    (dry-run; pass --apply to actually overwrite repo files)"
    fi
    log ""
    for skill_dir in "$REPO_ROOT"/skills/*/; do
        skill_name="$(basename "$skill_dir")"
        live="$HOME_DIR/.claude/skills/$skill_name"
        repo_target="$REPO_ROOT/skills/$skill_name"
        if [[ -L "$live" ]]; then
            note "$skill_name: live is a symlink → already in sync, skipping"
            continue
        fi
        if [[ ! -d "$live" ]]; then
            note "$skill_name: not present at $live, skipping"
            continue
        fi
        if diff -rq "$live" "$repo_target" >/dev/null 2>&1; then
            note "$skill_name: in sync, nothing to pull"
            continue
        fi
        note "$skill_name: DRIFT detected"
        diff -ru "$repo_target" "$live" 2>&1 | sed 's/^/    /' | head -60
        if [[ $APPLY -eq 1 ]]; then
            rm -rf "$repo_target"
            cp -R "$live" "$repo_target"
            note "$skill_name: ✓ pulled into repo. Commit with `git add -A skills/`."
        fi
    done
    log ""
    if [[ $APPLY -eq 0 ]]; then
        log "✅ Pull dry-run complete. Re-run with --pull-skills --apply to write."
    else
        log "✅ Pull complete. Review with `git diff` and commit."
    fi
    exit 0
fi

if [[ $APPLY -eq 0 ]]; then
    log "📋 DRY RUN — no changes will be made. Re-run with --apply to mutate."
else
    log "🔧 APPLYING changes from $REPO_ROOT"
fi
log ""

# --- helpers ----------------------------------------------------------------

# ensure_symlink <target_path> <expected_source>
# Creates target_path → expected_source, backing up anything in the way.
ensure_symlink() {
    local target="$1" expected="$2"
    local target_parent
    target_parent="$(dirname "$target")"

    if [[ ! -e "$expected" && ! -L "$expected" ]]; then
        warn "source missing: $expected (skipping $target)"
        return 1
    fi

    if [[ -L "$target" ]]; then
        local current
        current="$(readlink "$target")"
        if [[ "$current" == "$expected" ]]; then
            note "OK   $target → $expected"
            return 0
        fi
        note "FIX  $target → was: $current   now: $expected"
        if [[ $APPLY -eq 1 ]]; then
            rm "$target"
            ln -s "$expected" "$target"
        fi
        return 0
    fi

    if [[ -e "$target" ]]; then
        local backup="${target}.bak-${TS}"
        note "BACKUP $target → $backup, then symlink → $expected"
        if [[ $APPLY -eq 1 ]]; then
            mv "$target" "$backup"
            mkdir -p "$target_parent"
            ln -s "$expected" "$target"
        fi
        return 0
    fi

    note "NEW  $target → $expected"
    if [[ $APPLY -eq 1 ]]; then
        mkdir -p "$target_parent"
        ln -s "$expected" "$target"
    fi
}

# ensure_file_copy <target_path> <source_path>
# Copies source over target, backing up if target exists and differs.
ensure_file_copy() {
    local target="$1" source="$2"
    if [[ ! -f "$source" ]]; then
        warn "source file missing: $source"
        return 1
    fi
    if [[ -f "$target" ]] && cmp -s "$source" "$target"; then
        note "OK   $target (matches $source)"
        return 0
    fi
    if [[ -f "$target" ]]; then
        local backup="${target}.bak-${TS}"
        note "UPDATE $target (backup → $backup)"
        if [[ $APPLY -eq 1 ]]; then
            cp "$target" "$backup"
            cp "$source" "$target"
        fi
    else
        note "NEW   $target (← $source)"
        if [[ $APPLY -eq 1 ]]; then
            mkdir -p "$(dirname "$target")"
            cp "$source" "$target"
        fi
    fi
}

# launchctl_reload <label> <plist_path>
launchctl_reload() {
    local label="$1" plist="$2"
    if [[ ! -f "$plist" ]]; then
        warn "plist missing for $label at $plist"
        return 1
    fi
    if [[ $APPLY -eq 1 ]]; then
        launchctl bootout "gui/$UID/$label" 2>/dev/null || true
        launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null || \
            launchctl load "$plist"
        note "reloaded $label"
    else
        note "would reload $label (bootout + bootstrap $plist)"
    fi
}

# --- 1. Symlink code into ~/.claude/ ----------------------------------------
log "[1/4] Symlinking code into ~/.claude/"

ensure_symlink "$HOME_DIR/.claude/bin" "$REPO_ROOT/bin"

mkdir -p "$HOME_DIR/.claude/spawn-prompts"
ensure_symlink \
    "$HOME_DIR/.claude/spawn-prompts/prompt-triage-agent.md" \
    "$REPO_ROOT/prompts/prompt-triage-agent.md"

mkdir -p "$HOME_DIR/.claude/lessons"
ensure_symlink \
    "$HOME_DIR/.claude/lessons/active" \
    "$REPO_ROOT/lessons/active"
# The curator script also writes index.md and .usage.json under ~/.claude/lessons/.
# We let those stay in ~/.claude/ as runtime artifacts (not committed).

ensure_symlink \
    "$HOME_DIR/.claude/assistant-operating-guide.md" \
    "$REPO_ROOT/docs/assistant-operating-guide.md"

log ""

# --- 2. Copy skills ---------------------------------------------------------
# Skills are COPIED (not symlinked) so they're standalone artifacts you can
# `cp -r` to share with someone else. The trade-off: edits to the live copy
# at ~/.claude/skills/<name>/ don't auto-sync back to the repo. We detect
# this drift on dry-run and warn so you can either pull edits into the repo
# or re-run --apply to overwrite them.
#
# Backups of pre-existing live skills go to ~/.claude/skills-backups/, NOT
# ~/.claude/skills/, because Claude Code auto-discovers ANY directory under
# ~/.claude/skills/ as a skill — leaving .bak entries there pollutes the
# registry.
log "[2/4] Copying skills into ~/.claude/skills/"
mkdir -p "$HOME_DIR/.claude/skills" "$HOME_DIR/.claude/skills-backups"
for skill_dir in "$REPO_ROOT"/skills/*/; do
    skill_name="$(basename "$skill_dir")"
    target="$HOME_DIR/.claude/skills/$skill_name"
    expected="$REPO_ROOT/skills/$skill_name"

    # Stale symlink from a prior symlink-based install — remove and replace
    # with a copy. The symlinked content was the repo content, so no data
    # loss; we go straight to copy.
    if [[ -L "$target" ]]; then
        note "MIGRATE $target was a symlink — replacing with a copy"
        if [[ $APPLY -eq 1 ]]; then
            rm "$target"
            cp -R "$expected" "$target"
        fi
        continue
    fi

    # Live directory exists.
    if [[ -d "$target" ]]; then
        if diff -rq "$expected" "$target" >/dev/null 2>&1; then
            note "OK   $target (matches repo)"
            continue
        fi
        # Drift: live differs from repo. Warn loudly.
        backup="$HOME_DIR/.claude/skills-backups/${skill_name}.bak-${TS}"
        warn "DRIFT $target differs from $expected"
        note "       backing up live copy to $backup, then overwriting with repo version"
        note "       diff summary:"
        diff -rq "$expected" "$target" 2>&1 | sed 's/^/         /' | head -10
        if [[ $APPLY -eq 1 ]]; then
            mv "$target" "$backup"
            cp -R "$expected" "$target"
        fi
        continue
    fi

    # Live doesn't exist (or is a stray file).
    if [[ -e "$target" ]]; then
        backup="$HOME_DIR/.claude/skills-backups/${skill_name}.bak-${TS}"
        note "BACKUP $target → $backup, then copy from repo"
        if [[ $APPLY -eq 1 ]]; then
            mv "$target" "$backup"
            cp -R "$expected" "$target"
        fi
    else
        note "NEW  $target ← $expected (copy)"
        if [[ $APPLY -eq 1 ]]; then
            cp -R "$expected" "$target"
        fi
    fi
done
log ""

# --- 3. Copy LaunchAgent plists + reload only those that changed ----------
log "[3/4] Copying LaunchAgent plists into ~/Library/LaunchAgents/"
mkdir -p "$HOME_DIR/Library/LaunchAgents"
declare -a CHANGED_LABELS
for plist in "$REPO_ROOT"/launchagents/*.plist; do
    label="$(basename "$plist" .plist)"
    target="$HOME_DIR/Library/LaunchAgents/$(basename "$plist")"
    if [[ -f "$target" ]] && cmp -s "$plist" "$target"; then
        note "OK   $target (matches repo, no reload needed)"
    else
        ensure_file_copy "$target" "$plist"
        CHANGED_LABELS+=("$label")
    fi
done
log ""

# --- 4. Reload only the daemons whose plists actually changed --------------
log "[4/4] Reloading launchd agents that changed"
if [[ ${#CHANGED_LABELS[@]} -eq 0 ]]; then
    note "no plists changed — nothing to reload"
else
    for label in "${CHANGED_LABELS[@]}"; do
        launchctl_reload "$label" "$HOME_DIR/Library/LaunchAgents/${label}.plist"
    done
fi
log ""

# --- summary ----------------------------------------------------------------
if [[ $APPLY -eq 0 ]]; then
    log "✅ Dry-run complete. Re-run with --apply to make changes."
else
    log "✅ Install complete. Verify with:"
    log "   ls -la ~/.claude/bin"
    log "   launchctl list | grep com.mukuls"
    log "   stat -f '%Sm' ~/.claude/cache/world.json   # should refresh within 30s"
fi
