#!/usr/bin/env bash
# install.sh — wire the live system at ~/.claude/ + ~/Library/LaunchAgents/
# to the canonical code in this repo at ~/dev/assistant/.
#
# Idempotent: re-run it freely. Default is --dry-run; pass --apply to mutate.
#
# Strategy:
#   - Code (bin/, prompts/, lessons/) is symlinked from ~/.claude/* to this
#     repo, so edits go live without copying.
#   - LaunchAgent plists are COPIED into ~/Library/LaunchAgents/ (launchd does
#     not follow symlinks reliably) and then unloaded + reloaded.
#   - Skills are SYMLINKED per-name into ~/.claude/skills/<name> → repo's
#     skills/<name>/. The repo is the single source of truth: a pull is live
#     immediately, and the pulse self-update can never clobber a live edit by
#     re-copying (it used to — see Section 2). Other skills under
#     ~/.claude/skills/ are left untouched.
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
  --pull-skills     Pull edits from a live ~/.claude/skills/<name>/ directory
                    BACK into the repo. Only needed to recover edits made while
                    a skill was still a real directory (e.g. backups left by the
                    copy→symlink migration). Once a skill is symlinked, live
                    edits ARE repo edits, so this becomes a no-op for it.
                    Prints a unified diff in dry-run; run with --apply to copy.
  -h, --help        This help.

After --apply:
  - ~/.claude/bin                                → symlink → $REPO_ROOT/bin
  - legacy ~/.claude/spawn-prompts/prompt-{assistant,triage}-agent.md → REMOVED
    (old LLM-Assistant era; the mechanical pulse.py reads prompts/ directly)
  - lessons live in ~/.claude/CLAUDE.md (not in this repo). Curator: bin/assistant-curator.py
  - ~/.claude/skills/<name> → SYMLINK → $REPO_ROOT/skills/<name> (repo is truth)
  - ~/Library/LaunchAgents/com.assistant.{world-scanner,assistant-pulse,assistant-page,
       session-context-watcher,assistant-todo-server}.plist → COPIED
  - cmux session-restore (vendored): hooks/ → ~/.claude/hooks/ (symlinks),
       bin/cmux-restore-sessions.py → ~/.local/bin/cmux-restore-sessions,
       and ~/.claude/settings.json SessionStart/SessionEnd hooks patched in
  - launchd: kickstart -k each agent (load if not loaded)

Skills are symlinked (not copied), so the repo is the single source of truth:
in-place edits to a skill ARE repo edits, a pull is live immediately, and the
pulse self-update can never revert a live edit by re-copying. A pre-existing
real directory at the target is backed up to ~/.claude/skills-backups/ before
the symlink replaces it; recover edits from there with --pull-skills.

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
log "[1/5] Symlinking code into ~/.claude/"

ensure_symlink "$HOME_DIR/.claude/bin" "$REPO_ROOT/bin"

# Decommission legacy spawn-prompts links from the old LLM-Assistant era
# (prompt-assistant-agent.md, prompt-triage-agent.md). The mechanical
# pulse.py orchestrator does not need these — the only prompt it loads is
# observer-batch-prompt.md, which it reads directly from $REPO_ROOT/prompts/.
for legacy_prompt in \
    "$HOME_DIR/.claude/spawn-prompts/prompt-assistant-agent.md" \
    "$HOME_DIR/.claude/spawn-prompts/prompt-triage-agent.md"; do
    if [[ -L "$legacy_prompt" || -e "$legacy_prompt" ]]; then
        note "REMOVE legacy $legacy_prompt (no longer needed; pulse.py reads observer-batch-prompt.md directly)"
        if [[ $APPLY -eq 1 ]]; then
            rm -f "$legacy_prompt"
        fi
    fi
done

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

# --- 2. Symlink skills ------------------------------------------------------
# Skills are SYMLINKED (not copied) into ~/.claude/skills/<name> → the repo's
# skills/<name>/. A copy-based install silently reverted live edits: the pulse
# self-update runs `install.sh --apply` after any pull touching skills/, and
# the copy path overwrote ~/.claude/skills/<name>/ with the repo version,
# clobbering uncommitted in-place edits (this is exactly how the cleanup
# skill's no-close-workspace edit came back on 2026-06-05). Symlinks make the
# repo the single source of truth: a `git pull` is live immediately, with
# nothing to re-copy and nothing to clobber.
#
# Backups of pre-existing live skills go to ~/.claude/skills-backups/, NOT
# ~/.claude/skills/, because Claude Code auto-discovers ANY directory under
# ~/.claude/skills/ as a skill — leaving .bak entries there pollutes the
# registry. (ensure_symlink's default <target>.bak-<ts> would land inside
# ~/.claude/skills/, so we back up by hand here before symlinking.)
log "[2/5] Symlinking skills into ~/.claude/skills/"
mkdir -p "$HOME_DIR/.claude/skills" "$HOME_DIR/.claude/skills-backups"
for skill_dir in "$REPO_ROOT"/skills/*/; do
    skill_name="$(basename "$skill_dir")"
    target="$HOME_DIR/.claude/skills/$skill_name"
    expected="$REPO_ROOT/skills/$skill_name"

    # Already the correct symlink — nothing to do.
    if [[ -L "$target" ]]; then
        current="$(readlink "$target")"
        if [[ "$current" == "$expected" ]]; then
            note "OK   $target → $expected"
        else
            note "FIX  $target → was: $current   now: $expected"
            if [[ $APPLY -eq 1 ]]; then
                rm "$target"
                ln -s "$expected" "$target"
            fi
        fi
        continue
    fi

    # A real directory (copy from a prior copy-based install, or live edits).
    # Back it up out of the skills tree before replacing with a symlink so we
    # never destroy uncommitted work and never pollute the skill registry.
    if [[ -e "$target" ]]; then
        backup="$HOME_DIR/.claude/skills-backups/${skill_name}.bak-${TS}"
        if [[ -d "$target" ]] && diff -rq "$expected" "$target" >/dev/null 2>&1; then
            note "MIGRATE $target (copy matches repo) → symlink"
        else
            warn "MIGRATE $target differs from repo — backing up to $backup before symlinking"
            note "       (any uncommitted live edits are preserved in the backup;"
            note "        bring them into the repo with: install.sh --pull-skills)"
        fi
        if [[ $APPLY -eq 1 ]]; then
            mv "$target" "$backup"
            ln -s "$expected" "$target"
        fi
        continue
    fi

    # Nothing there — fresh symlink.
    note "NEW  $target → $expected (symlink)"
    if [[ $APPLY -eq 1 ]]; then
        ln -s "$expected" "$target"
    fi
done
log ""

# --- 3. Copy LaunchAgent plists + reload only those that changed ----------
# Plists in the repo may contain `/Users/<user>/...` as a home placeholder.
# At install time we substitute the live $HOME and stage the result in a
# temp dir, then copy that to ~/Library/LaunchAgents/. Plists that don't
# have the placeholder (the original 5 with literal /Users/mukuls) are
# unchanged by the sed.
log "[3/5] Copying LaunchAgent plists into ~/Library/LaunchAgents/"
mkdir -p "$HOME_DIR/Library/LaunchAgents"
PLIST_STAGE="$(mktemp -d)"
trap 'rm -rf "$PLIST_STAGE"' EXIT

# Opt-in plists the installer must NEVER copy or load. The single-process
# daemon (com.mukul.assistant-daemon) is additive: it replaces the pulse
# agent, so auto-loading it alongside the legacy pulse timer would run two
# pulse loops at once. It is activated by hand (see the plist's header
# comment). The pulse self-update runs `install.sh --apply` on any
# launchagents/ change, so this skip is what keeps a committed plist from
# auto-starting on the running box.
PLIST_SKIP=(
    "com.mukul.assistant-daemon.plist"
    # Keel M5 connectors are INDEPENDENT KeepAlive daemons that poll external
    # APIs (GitHub, Gmail) outside the pulse budget. The installer copies their
    # plists but NEVER loads them: load-bearing because the pulse self-update
    # re-runs install.sh, and an auto-load would start a network daemon (and,
    # for Gmail, begin OAuth refreshes) behind Mukul's back. He runs
    # `launchctl load …` by hand once the connector is configured.
    "com.assistant.connector-github.plist"
    "com.assistant.connector-gmail.plist"
    # M5 wave-2 connectors — same INDEPENDENT KeepAlive daemon contract as
    # wave-1 (copied, never auto-loaded; Mukul runs `launchctl load` by hand
    # once each source is configured). KeepAlive={SuccessfulExit:false}, NOT
    # KeepAlive=true, so an unconfigured daemon never hot-respawns (F3).
    "com.assistant.connector-gcal.plist"
    "com.assistant.connector-jira.plist"
    "com.assistant.connector-slack.plist"
)

declare -a CHANGED_LABELS
for plist in "$REPO_ROOT"/launchagents/*.plist; do
    base="$(basename "$plist")"
    label="$(basename "$plist" .plist)"
    staged="$PLIST_STAGE/$base"
    target="$HOME_DIR/Library/LaunchAgents/$base"

    skip=0
    for skip_base in "${PLIST_SKIP[@]}"; do
        [[ "$base" == "$skip_base" ]] && skip=1 && break
    done
    if [[ $skip -eq 1 ]]; then
        note "SKIP $base (opt-in daemon — activate by hand, see plist header)"
        continue
    fi

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

# --- 3b. Ensure the Assistant log dir exists --------------------------------
# Every Assistant LaunchAgent writes its launchd stdout/stderr capture (and the
# two watchers write their app-logs) into ~/.assistant/logs/. launchd will not
# create a missing StandardOutPath parent, so make sure the dir exists.
log "[3b] Ensuring ~/.assistant/logs/"
mkdir -p "$HOME_DIR/.assistant/logs"
note "ensured $HOME_DIR/.assistant/logs/"
log ""

# --- 4. cmux session-restore (vendored) -------------------------------------
# Three layers that make Claude panes survive cmux restart/reboot:
#   Layer 1+2  hooks/cmux-auto-resume.py + cmux-session-ledger.py
#              → symlinked into ~/.claude/hooks/ (a MIXED dir — symlink the
#                files individually, never the directory)
#   Layer 3    bin/cmux-restore-sessions.py → ~/.local/bin/cmux-restore-sessions
#   settings   install/patch-settings.py registers the SessionStart/SessionEnd
#              hook commands in ~/.claude/settings.json (idempotent, self-backs-up)
# Vendored from the former elitecoder/cmux-session-restore repo so a single
# `assistant install --apply` rebuilds all three layers from git on any machine
# — claude.tgz-style home backups only capture ~/.claude and silently drop the
# ~/.local/bin CLI, leaving a deceptive half-working state.
log "[4/5] Wiring cmux session-restore (hooks + CLI + settings)"
ensure_symlink "$HOME_DIR/.claude/hooks/cmux-auto-resume.py"    "$REPO_ROOT/hooks/cmux-auto-resume.py"
ensure_symlink "$HOME_DIR/.claude/hooks/cmux-session-ledger.py" "$REPO_ROOT/hooks/cmux-session-ledger.py"
ensure_symlink "$HOME_DIR/.local/bin/cmux-restore-sessions"     "$REPO_ROOT/bin/cmux-restore-sessions.py"
if [[ $APPLY -eq 1 ]]; then
    python3 "$REPO_ROOT/install/patch-settings.py" "$HOME_DIR/.claude/settings.json" \
        | sed 's/^/  /'
else
    note "would patch ~/.claude/settings.json (SessionStart: cmux-auto-resume + ledger start; SessionEnd: ledger end)"
fi
log ""

# --- 5. Reload only the daemons whose plists actually changed --------------
# When invoked by the pulse's own self-update (ASSISTANT_SELF_UPDATE=1), never
# reload the pulse's OWN plist: launchctl bootout would SIGTERM the running
# pulse.py mid-update and abort the install. The new plist still got copied;
# it applies on the next manual install or reboot. Plist changes are rare;
# code changes (symlinked bin/, no reload needed) are the common case.
SELF_PLIST_LABEL="com.assistant.assistant-pulse"
log "[5/5] Reloading launchd agents that changed"
if [[ ${#CHANGED_LABELS[@]} -eq 0 ]]; then
    note "no plists changed — nothing to reload"
else
    for label in "${CHANGED_LABELS[@]}"; do
        if [[ "${ASSISTANT_SELF_UPDATE:-0}" == "1" && "$label" == "$SELF_PLIST_LABEL" ]]; then
            note "skip reload of $label (self-update can't bootout its own pulse; applies on next reboot/manual install)"
            continue
        fi
        launchctl_reload "$label" "$HOME_DIR/Library/LaunchAgents/${label}.plist"
    done
fi
log ""

# --- 6. cmux-watcher LaunchAgent (opt-in: written, NEVER auto-loaded) -------
# The cmux-watcher taps `cmux events --category agent --reconnect` and drops
# workspace signals into ~/.assistant/inbox within seconds. Per the global
# CLAUDE.md lesson ("Always ask before running launchctl load"), this installer
# WRITES the plist but never loads it — it prints the load command for Mukul to
# run by hand.
log "[6/6] Writing cmux-watcher LaunchAgent plist (NOT loaded)"
WATCHER_PLIST="$HOME_DIR/Library/LaunchAgents/com.mukul.assistant-cmux-watcher.plist"
WATCHER_PY="/opt/homebrew/bin/python3"
[[ -x "$WATCHER_PY" ]] || WATCHER_PY="/usr/bin/python3"
WATCHER_PLIST_BODY="$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.mukul.assistant-cmux-watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>$WATCHER_PY</string>
        <string>$REPO_ROOT/bin/cmux-watcher.py</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$HOME_DIR/.assistant/cmux-watcher.log</string>
    <key>StandardErrorPath</key><string>$HOME_DIR/.assistant/cmux-watcher-error.log</string>
</dict>
</plist>
PLIST
)"
if [[ $APPLY -eq 1 ]]; then
    printf '%s\n' "$WATCHER_PLIST_BODY" > "$WATCHER_PLIST"
    note "wrote $WATCHER_PLIST"
else
    note "would write $WATCHER_PLIST"
fi
note "cmux-watcher is OPT-IN — NOT loaded automatically."
note "To activate it yourself:"
note "    launchctl load $WATCHER_PLIST"
log ""

# --- 7. Memory setup -----------------------------------------------------------
# Two paths:
#   A) Owner machine (Mukul): clone the private mukul-memory repo and run its
#      install.sh to sync all memories, lessons, and Obsidian notes across machines.
#   B) Other user: set up local-only memory (Mem0 with local fastembed, no cross-machine
#      sync). Lessons stay in ~/.claude/CLAUDE.md; semantic memory lives at
#      ~/.assistant/mem0/ on this machine only.
#
# The installer asks interactively only when --apply is set; in dry-run it explains
# what each path would do.
log "[7/7] Memory setup"

MEMORY_CONFIG="$HOME_DIR/.assistant/memory-repo-config.json"

if [[ -f "$MEMORY_CONFIG" ]]; then
    note "OK   memory already configured ($MEMORY_CONFIG exists)"
else
    if [[ $APPLY -eq 0 ]]; then
        note "(dry-run) Would ask: is this the owner's machine or a new user's?"
        note "  Owner path: clone git@github.com:OneAdobe/mukul-memory + run scripts/install.sh"
        note "  Other user path: initialize local-only memory at ~/.assistant/mem0/"
    else
        log ""
        # Non-interactive (self-update, CI, pipe): skip memory setup silently.
        # Memory config is a one-time human decision — auto-update must never
        # block waiting for input or overwrite an existing choice.
        if [[ ! -t 0 ]]; then
            note "non-interactive run — skipping memory setup (run install.sh --apply manually to configure)"
            log ""
            # Jump to summary by skipping the case block
            MEM_CHOICE=3
        else

        log "Memory is not configured on this machine. Choose a setup:"
        log "  1) Owner machine (Mukul) — sync memories, lessons, Obsidian notes from the private mukul-memory repo"
        log "  2) New user — set up local-only memory (no cross-machine sync)"
        log "  3) Skip — set up memory manually later"
        log ""
        read -r -p "Choice [1/2/3]: " MEM_CHOICE

        fi  # end interactive block

        case "$MEM_CHOICE" in
            1)
                MEMORY_REPO_DIR="$HOME_DIR/dev/mukul-memory"
                if [[ -d "$MEMORY_REPO_DIR/.git" ]]; then
                    note "mukul-memory already cloned at $MEMORY_REPO_DIR"
                else
                    log "  Cloning mukul-memory…"
                    git clone "git@github.com:OneAdobe/mukul-memory.git" "$MEMORY_REPO_DIR" \
                        && note "cloned to $MEMORY_REPO_DIR" \
                        || { warn "clone failed — check your git@github.com (work) SSH key"; }
                fi
                if [[ -d "$MEMORY_REPO_DIR/.git" ]]; then
                    log "  Running memory install (sync-pull: lessons + mem0 + Obsidian)…"
                    bash "$MEMORY_REPO_DIR/scripts/install.sh" \
                        && note "memory synced from mukul-memory repo" \
                        || warn "memory install had errors — check $MEMORY_REPO_DIR/scripts/install.sh"
                fi
                ;;
            2)
                log "  Initializing local-only memory…"
                mkdir -p "$HOME_DIR/.assistant/mem0"
                # Write a minimal config that disables cross-machine sync
                cat > "$MEMORY_CONFIG" <<LOCAL_CFG
{
  "memory_repo": null,
  "sync": {
    "push_on_lesson_confirm": false,
    "push_on_memory_add": false,
    "pull_interval_seconds": 0
  },
  "stores": {
    "lessons_file": null,
    "memories_file": null,
    "chroma_dir": "~/.assistant/mem0/chroma",
    "claude_md": "~/.claude/CLAUDE.md"
  }
}
LOCAL_CFG
                note "wrote $MEMORY_CONFIG (local-only mode)"
                note "Semantic memory will build up locally as you use the assistant."
                note "To enable cross-machine sync later: set up a private git repo,"
                note "then update $MEMORY_CONFIG with the repo URL."
                ;;
            3)
                note "Skipped. Run install.sh --apply again when ready to configure memory."
                ;;
            *)
                warn "Unknown choice '$MEM_CHOICE' — skipping memory setup."
                ;;
        esac
    fi
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
