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
# Opt-in feature daemons: off by default (core-only install). Each --with flag
# turns one on. See the "Daemon tiers" block in Section 3.
WITH_MEMORY=0
WITH_CRASH_RESUME=0
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
        --with-memory) WITH_MEMORY=1 ;;
        --with-crash-resume) WITH_CRASH_RESUME=1 ;;
        --with-all) WITH_MEMORY=1; WITH_CRASH_RESUME=1 ;;
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

  Opt-in feature daemons (off by default — a bare --apply installs CORE only:
  the fleet loop pulse/world-scanner/session-context-watcher/assistant-page/
  todo-server). On an interactive --apply (a real terminal), install.sh OFFERS
  each undecided feature with a one-line explanation and a [y/N] prompt, so you
  discover them without reading docs. Your answer is remembered in
  ~/.assistant/feature-opt-in, so re-runs never re-ask. Headless runs (pulse
  self-update, curl|bash, CI) never prompt — they default to OFF.
  --with-memory        cross-machine memory sync (memory-sync-pull). Only useful
                       if you run Assistant on more than one machine.
  --with-crash-resume  auto-resume crashed cmux workspaces (workspace-watcher).
  --with-all           enable all of the above (and suppress their prompts).
  (Slack comms + slack-reactor are always copied but hand-loaded after their
   token setup — see ONBOARDING.md — never auto-loaded even with a flag.)
  Changed your mind? Declined a feature and want it now: re-run with its --with
  flag (the flag beats a remembered 'no'). To be re-asked from scratch: delete
  its line from ~/.assistant/feature-opt-in. To disable a running one:
  launchctl bootout gui/\$UID/com.assistant.<label>, then set its line to 'no'.
  Flags/answers only affect what gets LOADED; an already-running feature daemon
  is never torn out (it's adopted and remembered as enabled).
  -h, --help        This help.

After --apply:
  - ~/.claude/bin                                → symlink → $REPO_ROOT/bin
  - legacy ~/.claude/spawn-prompts/prompt-{assistant,triage}-agent.md → REMOVED
    (old LLM-Assistant era; the mechanical pulse.py reads prompts/ directly)
  - lessons live in ~/.claude/CLAUDE.md (not in this repo). Curator: bin/assistant-curator.py
  - ~/.claude/skills/<name> → SYMLINK → $REPO_ROOT/skills/<name> (repo is truth)
  - CORE plists loaded: com.assistant.{assistant-pulse,world-scanner,
       session-context-watcher,assistant-page,assistant-todo-server}
  - FEATURE plists copied, loaded only with their --with flag: memory-sync-pull
       (--with-memory), workspace-watcher (--with-crash-resume); comms +
       slack-reactor copied but hand-loaded after token setup
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

# --- feature opt-in resolution ----------------------------------------------
# Turn the WITH_* flags into final load decisions, offering a DISCOVERY PROMPT
# for undecided features on an interactive --apply. Precedence per feature
# (first match wins): explicit --with flag > remembered answer > daemon already
# loaded > interactive prompt > headless default-NO.
#
# Guard lineage: mirrors the phase-7 memory step ([[ ! -t 0 ]] skip + a state
# file for idempotence). A prompt fires ONLY on an interactive --apply that is
# NOT a self-update; all headless contexts (pulse self-update, curl|bash, CI)
# fall through to default-NO WITHOUT persisting, so discovery survives to the
# user's first real interactive run. NEVER hangs: no bare `read` (set -e would
# abort on EOF), and a walk-away is bounded by read -t.
STATE_FILE="$HOME_DIR/.assistant/feature-opt-in"

# state_get <feat> → prints yes|no|"" (empty = never decided). `local` masks
# grep's exit-1-on-no-match so set -e can't abort.
state_get() {
    local v
    v="$(grep "^$1=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2)"
    printf '%s' "$v"
}
# state_set <feat> <yes|no> — APPLY-only, atomic last-write-wins. The `|| true`
# after grep -v is mandatory (no-match exits 1 under set -e).
state_set() {
    [[ $APPLY -eq 1 ]] || return 0
    mkdir -p "$(dirname "$STATE_FILE")"
    touch "$STATE_FILE"
    local tmp="$STATE_FILE.tmp.$$"
    { grep -v "^$1=" "$STATE_FILE" 2>/dev/null || true; printf '%s=%s\n' "$1" "$2"; } > "$tmp" \
        && mv "$tmp" "$STATE_FILE"
}
explicit_flag_for() {
    case "$1" in
        memory)        echo "$WITH_MEMORY" ;;
        crash-resume)  echo "$WITH_CRASH_RESUME" ;;
        *)             echo 0 ;;
    esac
}
_feat_label() {
    case "$1" in
        memory)        echo "com.assistant.memory-sync-pull" ;;
        crash-resume)  echo "com.assistant.workspace-watcher" ;;
    esac
}
# daemon_loaded <feat> — 0 if the feature's LaunchAgent is loaded. Called ONLY
# inside a condition (never bare) so its nonzero-when-absent can't trip set -e.
daemon_loaded() { launchctl print "gui/$UID/$(_feat_label "$1")" >/dev/null 2>&1; }
feat_desc() {
    case "$1" in
        memory)        echo "Memory sync keeps your lessons + semantic memory identical across all your machines (needs a private git repo; only useful on 2+ machines). Enable? [y/N] " ;;
        crash-resume)  echo "Crash-resume auto-restarts a cmux workspace whose Claude session died, so long jobs survive a crash or reboot. Enable? [y/N] " ;;
    esac
}
# prompt_yn <question> → 0 on yes. Sets PROMPT_TIMED_OUT=1 iff read timed out
# (exit >128) so the caller can distinguish a walk-away (do NOT persist) from an
# explicit decline (persist no). The if/else wrapper is mandatory: a bare read
# returns nonzero on EOF and set -e would abort the whole install.
PROMPT_TIMED_OUT=0
prompt_yn() {
    local q="$1" ans rc
    PROMPT_TIMED_OUT=0
    if read -r -t 60 -p "$q" ans; then rc=0; else rc=$?; fi
    # bash: read exits >128 specifically on -t timeout; EOF exits 1.
    if [[ ${rc:-0} -gt 128 ]]; then PROMPT_TIMED_OUT=1; ans=""; fi
    case "$ans" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *)                 return 1 ;;
    esac
}
# feature_should_prompt <feat> — the full guard, all terms AND-ed. Each headless
# context is blocked by ≥2 independent terms (self-update fails both -t 0 AND the
# env term; curl|bash & CI fail -t 0).
feature_should_prompt() {
    [[ $APPLY -eq 1 ]] || return 1
    [[ -t 0 ]] || return 1
    [[ "${ASSISTANT_SELF_UPDATE:-0}" != "1" ]] || return 1
    [[ "$(explicit_flag_for "$1")" != "1" ]] || return 1
    [[ -z "$(state_get "$1")" ]] || return 1
    ! daemon_loaded "$1" || return 1
    return 0
}

_optin_header_shown=0
_optin_header() {
    [[ $_optin_header_shown -eq 1 ]] && return 0
    _optin_header_shown=1
    log "[2.5] Optional features (safe to skip; enable later with --with-<name>)"
}
for _pair in 'memory:WITH_MEMORY' 'crash-resume:WITH_CRASH_RESUME'; do
    _feat="${_pair%%:*}"; _var="${_pair##*:}"
    if [[ "$(explicit_flag_for "$_feat")" == "1" ]]; then
        state_set "$_feat" yes                      # (a) explicit flag wins
        continue
    fi
    _saved="$(state_get "$_feat")"
    if [[ "$_saved" == "yes" ]]; then eval "$_var=1"; continue; fi   # (b) remembered yes
    if [[ "$_saved" == "no"  ]]; then continue; fi                  # (b) remembered no
    if daemon_loaded "$_feat"; then                                # (c) adopt already-running
        eval "$_var=1"; state_set "$_feat" yes; continue
    fi
    if feature_should_prompt "$_feat"; then                        # (d) discovery prompt
        _optin_header
        if prompt_yn "$(feat_desc "$_feat")"; then
            eval "$_var=1"; state_set "$_feat" yes
        elif [[ $PROMPT_TIMED_OUT -eq 1 ]]; then
            note "$_feat: no response (timed out) — left undecided, will re-ask next time"
        else
            state_set "$_feat" no                  # explicit decline → remember, stop nagging
        fi
    fi
    # (e) headless-undecided: leave WITH at 0, DO NOT persist (preserve discovery)
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
    # Symlink guard: NEVER cp through a symlink. A legacy install left some
    # ~/Library/LaunchAgents/*.plist as symlinks INTO the repo (e.g.
    # workspace-watcher). macOS cp follows the link and would write the rendered,
    # /Users/mukuls-substituted content back through it into the committed
    # template — corrupting the portable template and dirtying the tree. Replace
    # any such symlink with a real file. (-e is false for a dangling symlink, so
    # test -L explicitly.)
    if [[ -L "$target" ]]; then
        note "REPLACE symlink $target → real rendered file (was → $(readlink "$target"))"
        if [[ $APPLY -eq 1 ]]; then
            rm -f "$target"
        fi
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

# --- 0. Preflight (assistant-doctor) ----------------------------------------
# Run the doctor BEFORE any mutation so a missing prerequisite is a clear,
# actionable error rather than a half-wired system. CORE failures (python, repo,
# git, cmux) abort an interactive --apply; optional-feature failures (Slack /
# warm session) only warn. Under the pulse self-update (ASSISTANT_SELF_UPDATE=1)
# we report-only and never block — the running box already passed core once.
log "[0] Preflight — assistant-doctor"
DOCTOR="$REPO_ROOT/bin/assistant-doctor.py"
DOCTOR_PY="$(command -v python3 || echo /usr/bin/python3)"
if [[ -f "$DOCTOR" ]]; then
    if "$DOCTOR_PY" "$DOCTOR" --only core; then
        note "core preflight passed"
    else
        if [[ "${ASSISTANT_SELF_UPDATE:-0}" == "1" ]]; then
            warn "core preflight FAILED (self-update: report-only, not blocking)"
        elif [[ $APPLY -eq 1 ]]; then
            warn "core preflight FAILED — aborting --apply. Fix the ↳ items above and re-run."
            exit 1
        else
            warn "core preflight FAILED (dry-run: not blocking; --apply would abort)"
        fi
    fi
else
    warn "assistant-doctor.py not found at $DOCTOR — skipping preflight"
fi
log ""

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

# --- 3. Generate LaunchAgent plists from templates + reload only those that changed ----------
# The committed launchagents/*.plist files are TEMPLATES: they carry four
# machine-independent tokens that we substitute with this box's real values at
# install time, staging the result in a temp dir before copying to
# ~/Library/LaunchAgents/. This replaced an earlier `/Users/<user>/`+sed scheme
# that was a silent no-op (the plists held literal /Users/mukuls, which the sed
# never matched, so every daemon shipped the author's home path and died on any
# other machine — the P1 onboarding bug).
#
# Tokens (see any launchagents/*.plist):
#   __HOME__    → this user's home ($HOME_DIR)
#   __REPO__    → this checkout ($REPO_ROOT), NOT assumed to be ~/dev/assistant
#   __PYTHON__  → an arch-resolved python3 (Apple-Silicon /opt/homebrew vs Intel
#                 /usr/local vs system /usr/bin) — a literal path would 404 on
#                 the other arch
#   __PATH__    → an arch-aware PATH superset incl. the Homebrew bin + cmux
#
# The filenames are deliberately UNCHANGED (still <label>.plist): self_update.py
# keys plist-change detection + its own-reload-defer guard on the exact string
# `launchagents/<label>.plist`, and its tests hardcode these names. Keeping the
# templates AT those paths means self-update keeps working.
log "[3/5] Generating LaunchAgent plists into ~/Library/LaunchAgents/"
mkdir -p "$HOME_DIR/Library/LaunchAgents"
PLIST_STAGE="$(mktemp -d)"
trap 'rm -rf "$PLIST_STAGE"' EXIT

# Resolve this machine's interpreter + PATH ONCE. Probe for a real python3
# rather than hardcode an arch: Homebrew (arm64 /opt/homebrew, Intel
# /usr/local), then system. Fail loud if none — a daemon with no interpreter is
# worse than a clear error.
BREW_BIN=""
if command -v brew >/dev/null 2>&1; then
    BREW_BIN="$(brew --prefix 2>/dev/null)/bin"
fi
PLIST_PYTHON=""
for cand in "$BREW_BIN/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -n "$cand" && -x "$cand" ]]; then PLIST_PYTHON="$cand"; break; fi
done
if [[ -z "$PLIST_PYTHON" ]]; then
    warn "no python3 found for LaunchAgent plists — install Xcode CLT or Homebrew python3"
    PLIST_PYTHON="/usr/bin/python3"  # last resort; doctor (phase 0) flags a missing one
fi
# PATH superset: real Homebrew bin (if any) first, then the standard dirs + the
# cmux CLI dir the watchers/comms need.
PLIST_PATH_VALUE="${BREW_BIN:+$BREW_BIN:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Applications/cmux.app/Contents/Resources/bin"
note "plist interpreter: $PLIST_PYTHON"

# Opt-in plists the installer must NEVER copy or load. The single-process
# daemon (com.mukul.assistant-daemon) is additive: it replaces the pulse
# agent, so auto-loading it alongside the legacy pulse timer would run two
# pulse loops at once. It is activated by hand (see the plist's header
# comment). The pulse self-update runs `install.sh --apply` on any
# launchagents/ change, so this skip is what keeps a committed plist from
# auto-starting on the running box.
PLIST_SKIP=(
    "com.mukul.assistant-daemon.plist"
)

# ── Daemon tiers ────────────────────────────────────────────────────────────
# CORE (everything not listed below) is loaded by default — the fleet loop +
# its dashboard: pulse, world-scanner, session-context-watcher, assistant-page,
# todo-server. There is no product without these.
#
# FEATURE daemons are always COPIED (so a later hand-load / --with flag works
# from the canonical path) but loaded ONLY when the user opts in. Each solves a
# problem not every user has, so none is forced on a fresh install:
#   memory-sync-pull   cross-machine memory sync   → --with-memory
#   workspace-watcher  auto-resume crashed cmux ws → --with-crash-resume
#   assistant-comms    Slack comms (needs token + assistant-comms-setup.sh)
#   slack-reactor      Slack emoji→todo (needs SLACK_APP_TOKEN/SLACK_BOT_TOKEN)
# comms/slack-reactor are never auto-loaded even with a flag — they crash-loop
# without their tokens, so they activate by hand after setup. memory &
# crash-resume DO auto-load when their flag is passed.
#
# feature_of <plist-base> → prints the feature name, or "" if it's core.
feature_of() {
    case "$1" in
        com.assistant.memory-sync-pull.plist)   echo "memory" ;;
        com.assistant.workspace-watcher.plist)  echo "crash-resume" ;;
        com.assistant.assistant-comms.plist)    echo "comms" ;;
        com.assistant.slack-reactor.plist)      echo "slack-reactor" ;;
        *)                                       echo "" ;;
    esac
}
# feature_enabled <feature> → 0 (load it) / 1 (copy-no-load). comms &
# slack-reactor are ALWAYS copy-no-load (token-gated); memory & crash-resume
# load only when their --with flag was passed.
feature_enabled() {
    case "$1" in
        memory)        [[ $WITH_MEMORY -eq 1 ]] ;;
        crash-resume)  [[ $WITH_CRASH_RESUME -eq 1 ]] ;;
        *)             return 1 ;;
    esac
}

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

    # Tier: CORE loads; a FEATURE daemon loads only if opted in, else copy-no-load.
    copy_no_load=0
    feat="$(feature_of "$base")"
    if [[ -n "$feat" ]]; then
        if feature_enabled "$feat"; then
            note "FEATURE $base — enabled (flag / prompt / remembered / already-running) → will load"
        else
            copy_no_load=1
            case "$feat" in
                memory|crash-resume)
                    note "FEATURE $base — not enabled (copied, NOT loaded; enable with --with-$feat)" ;;
                *)  # comms / slack-reactor: token-gated, hand-loaded after setup
                    note "FEATURE $base — token-gated (copied, NOT loaded; hand-load after setup — see ONBOARDING.md)" ;;
            esac
        fi
    fi

    # Substitute the four machine tokens with this box's real values. Order:
    # __REPO__ before __HOME__ is irrelevant (distinct tokens), but we do all
    # four so no placeholder ever reaches launchd. A surviving __TOKEN__ is a
    # loud plutil/launchd failure, not a silent wrong-path — the opposite of the
    # old no-op sed.
    sed -e "s|__PYTHON__|$PLIST_PYTHON|g" \
        -e "s|__REPO__|$REPO_ROOT|g" \
        -e "s|__HOME__|$HOME_DIR|g" \
        -e "s|__PATH__|$PLIST_PATH_VALUE|g" \
        "$plist" > "$staged"

    # A symlinked target (legacy install pointing into the repo) must be turned
    # into a real file regardless of content — cmp reads THROUGH the link, so
    # never trust an "OK match" on a symlink. Force the ensure_file_copy path.
    if [[ ! -L "$target" && -f "$target" ]] && cmp -s "$staged" "$target"; then
        note "OK   $target (matches repo, no reload needed)"
    else
        ensure_file_copy "$target" "$staged"
        # Copy-no-load plists are staged but never added to CHANGED_LABELS, so
        # Section 5 never reloads them — they wait for a hand-load.
        if [[ $copy_no_load -eq 1 ]]; then
            note "     (opt-in comms daemon — copied but NOT loaded; hand-load after setup)"
        else
            CHANGED_LABELS+=("$label")
        fi
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

# --- 3b. Migrate LaunchAgent logs → ~/.assistant/logs/ ----------------------
# Assistant launchd captures + the two watchers' app-logs historically landed
# in ~/.architect/orchestrator-logs/ (the orchestrator's home, a DIFFERENT
# system). The plists and scripts now point at ~/.assistant/logs/; this ensures
# the dir exists and migrates any files the old paths left behind. The migration
# script is idempotent and only moves Assistant-OWNED files (it leaves the
# orchestrator's own logs in place). It NEVER runs launchctl — Section 5 below
# reloads the changed agents, so we pass --no-launchctl-hint to suppress its own
# manual-reload block. In dry-run we ask the migrator to print its plan too.
log "[3b] Ensuring ~/.assistant/logs/ and migrating stray Assistant logs"
mkdir -p "$HOME_DIR/.assistant/logs"
note "ensured $HOME_DIR/.assistant/logs/"
MIGRATE_SCRIPT="$REPO_ROOT/bin/migrate-logs-to-assistant.sh"
if [[ -x "$MIGRATE_SCRIPT" ]]; then
    if [[ $APPLY -eq 1 ]]; then
        bash "$MIGRATE_SCRIPT" --apply --no-launchctl-hint | sed 's/^/  /'
    else
        bash "$MIGRATE_SCRIPT" --no-launchctl-hint | sed 's/^/  /'
    fi
else
    warn "migration script missing or not executable: $MIGRATE_SCRIPT"
fi
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
# Reuse the arch-resolved interpreter from Section 3 (falls back if this section
# is ever reached standalone).
WATCHER_PY="${PLIST_PYTHON:-/usr/bin/python3}"
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
        note "  Owner path: clone git@github-personal:elitecoder/mukul-memory + run scripts/install.sh"
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
                    git clone "git@github-personal:elitecoder/mukul-memory.git" "$MEMORY_REPO_DIR" \
                        && note "cloned to $MEMORY_REPO_DIR" \
                        || { warn "clone failed — check your git@github-personal SSH key"; }
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

# --- summary + NEXT STEPS runbook -------------------------------------------
# On --apply the installer RELOADS the always-on set whose plists changed
# (phase [5]) — those are on by running the installer. The OPT-IN daemons
# (cmux-watcher, comms, slack-reactor, the single-process daemon) are
# copy-no-load / skip and must be hand-loaded. So the summary states what is
# already running vs. what the user still activates by hand. Same runbook in
# ONBOARDING.md (linked from the README).
if [[ $APPLY -eq 0 ]]; then
    log "✅ Dry-run complete. Re-run with --apply to make changes."
    log "   Then follow the activation runbook it prints (also in ONBOARDING.md)."
else
    log "✅ Install complete. The always-on set (pulse orchestrator, dashboard"
    log "   page, todo-server, session-context-watcher, workspace-watcher,"
    log "   world-scanner) has been (re)loaded by this --apply. Verify:"
    log "     launchctl list | grep com.assistant"
    log "     stat -f '%Sm' ~/.claude/cache/world.json   # refreshes within ~30s"
    log ""
    log "   OPT-IN features are copied but NOT loaded — enable the ones you want:"
    log ""
    log "   ── workspace-signal pings (needed for comms 'needs input') ────────"
    log "   • launchctl load ~/Library/LaunchAgents/com.mukul.assistant-cmux-watcher.plist"
    log ""
    log "   ── Slack comms (bidirectional; needs the cmux-watcher above) ──────"
    log "   • Set SLACK_BOT_TOKEN in ~/.zprofile, create a private Slack channel,"
    log "     /invite the bot, then:  ./bin/assistant-comms-setup.sh"
    log "     (it runs a preflight and prints the exact launchctl line when green)"
    log ""
    log "   ── Slack emoji → todo capture ─────────────────────────────────────"
    log "   • Set SLACK_APP_TOKEN + SLACK_BOT_TOKEN in ~/.zprofile, then:"
    log "     launchctl load ~/Library/LaunchAgents/com.assistant.slack-reactor.plist"
    log ""
    log "   Re-run anytime:  ./bin/assistant-doctor.py         (preflight health)"
    log "   Full guide:      ONBOARDING.md"
fi
