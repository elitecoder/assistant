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
  - ~/.claude/spawn-prompts/prompt-assistant-agent.md → symlink → $REPO_ROOT/prompts/...
  - lessons live in ~/.claude/CLAUDE.md (not in this repo). Curator: bin/assistant-curator.py
  - ~/.claude/skills/{todo,cleanup,spawn-claude-workspace} → COPIES (shareable)
  - ~/Library/LaunchAgents/com.assistant.{world-scanner,assistant-pulse,assistant-page,
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
    "$HOME_DIR/.claude/spawn-prompts/prompt-assistant-agent.md" \
    "$REPO_ROOT/prompts/prompt-assistant-agent.md"

# Clean up the legacy symlink from before the Triage→Assistant rename. Eval
# runners and any in-flight Assistant workspace still reference the old path
# in their loaded prompt; leaving a dangling symlink would silently fail.
LEGACY_PROMPT_LINK="$HOME_DIR/.claude/spawn-prompts/prompt-triage-agent.md"
if [[ -L "$LEGACY_PROMPT_LINK" ]]; then
    note "REMOVE legacy $LEGACY_PROMPT_LINK (replaced by prompt-assistant-agent.md)"
    if [[ $APPLY -eq 1 ]]; then
        rm "$LEGACY_PROMPT_LINK"
    fi
fi

# Lessons live inside ~/.claude/CLAUDE.md as a `## Lessons` section. CLAUDE.md
# is officially auto-loaded by Claude Code into every session, so any agent
# (this Assistant, an ad-hoc claude session, the per-ws observer subagents) sees the
# rules without explicit injection. Each user maintains their own. The
# curator at bin/assistant-curator.py reads/writes that section.
#
# Decommission legacy lesson stores if present:
#   ~/.claude/lessons/        — pre-2026-05-23 location (symlinked into repo)
#   ~/.assistant/lessons/     — 2026-05-23 location (JSON sidecar)
for legacy_lessons in "$HOME_DIR/.claude/lessons" "$HOME_DIR/.assistant/lessons"; do
    if [[ -e "$legacy_lessons" ]]; then
        note "REMOVE legacy $legacy_lessons (lessons now live in ~/.claude/CLAUDE.md)"
        if [[ $APPLY -eq 1 ]]; then
            rm -rf "$legacy_lessons"
        fi
    fi
done

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
        # `diff -rq` returns rc=1 when files differ — that's expected and not an
        # error. Combined with `head`'s early-close SIGPIPE, the pipeline returns
        # nonzero, and under `set -euo pipefail` that aborts the loop iteration
        # BEFORE the mv+cp can run. Wrap with `|| true` to swallow the expected
        # nonzero exit while keeping the diff output visible.
        (diff -rq "$expected" "$target" 2>&1 | sed 's/^/         /' | head -10) || true
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
# Plists in the repo may contain `/Users/<user>/...` as a home placeholder.
# At install time we substitute the live $HOME and stage the result in a
# temp dir, then copy that to ~/Library/LaunchAgents/. Plists that don't
# have the placeholder (the original 5 with literal /Users/mukuls) are
# unchanged by the sed.
log "[3/4] Copying LaunchAgent plists into ~/Library/LaunchAgents/"
mkdir -p "$HOME_DIR/Library/LaunchAgents"
PLIST_STAGE="$(mktemp -d)"
trap 'rm -rf "$PLIST_STAGE"' EXIT

declare -a CHANGED_LABELS
for plist in "$REPO_ROOT"/launchagents/*.plist; do
    base="$(basename "$plist")"
    label="$(basename "$plist" .plist)"
    staged="$PLIST_STAGE/$base"
    target="$HOME_DIR/Library/LaunchAgents/$base"

    # Substitute /Users/<user>/ → $HOME/ so the staged plist references the
    # current user's home dir. No-op for plists that don't have the token.
    sed "s|/Users/<user>/|$HOME_DIR/|g" "$plist" > "$staged"

    if [[ -f "$target" ]] && cmp -s "$staged" "$target"; then
        note "OK   $target (matches repo, no reload needed)"
    else
        ensure_file_copy "$target" "$staged"
        CHANGED_LABELS+=("$label")
    fi
done

# Tear down legacy plists (renames + removed services).
#  - com.mukuls.triage-pulse → com.assistant.assistant-pulse (2026-05-23 rename)
#  - com.mukuls.{assistant-pulse,assistant-page,assistant-todo-server,
#    session-context-watcher,world-scanner} → com.assistant.* (2026-05-23 namespacing)
# Leaving any of these loaded would mean two daemons running the same job in
# parallel after install. Always unload + remove on apply.
LEGACY_LABELS=(
    "com.mukuls.triage-pulse"
    "com.mukuls.assistant-pulse"
    "com.mukuls.assistant-page"
    "com.mukuls.assistant-todo-server"
    "com.mukuls.session-context-watcher"
    "com.mukuls.world-scanner"
)
for legacy in "${LEGACY_LABELS[@]}"; do
    legacy_plist="$HOME_DIR/Library/LaunchAgents/${legacy}.plist"
    if [[ -f "$legacy_plist" ]]; then
        note "REMOVE legacy LaunchAgent $legacy (renamed → com.assistant.*)"
        if [[ $APPLY -eq 1 ]]; then
            launchctl bootout "gui/$UID/$legacy" 2>/dev/null || true
            rm "$legacy_plist"
        fi
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
    log "   launchctl list | grep com.assistant"
    log "   stat -f '%Sm' ~/.claude/cache/world.json   # should refresh within 30s"
fi
