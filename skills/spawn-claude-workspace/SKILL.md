---
name: spawn-claude-workspace
description: Spawn a new cmux workspace in the background, start a fresh Claude Code instance inside it, and auto-deliver a user-supplied prompt. Focus stays on the current workspace. Use when the user wants to kick off a parallel Claude task in a new workspace, branch a long-running job off into its own terminal, or dispatch a background prompt without losing their current context.
---

# spawn-claude-workspace

Spawn a new cmux workspace **in the background**, launch `claude` inside it, auto-send the user's prompt, and return focus to the originating workspace.

## Calibration notes (verified empirically on this machine, latest 2026-05-14)

These details override anything you might infer from `cmux --help`. The cmux CLI shortcuts (`cmux send`, `cmux read-screen`, `cmux paste-buffer`) reject terminal surfaces with `Error: invalid_params: Surface is not a terminal` even when the surface is clearly a terminal with a live tty. The RPC path works fine. **Use `cmux rpc surface.*` for every terminal I/O operation.**

**CRITICAL — default to `--focus false` to avoid stealing Mukul's foreground.** Re-verified 2026-05-23 on current cmux build: `cmux new-workspace --cwd <path> --name <title> --command "..."` WITHOUT `--focus true` produces a fully functional workspace with a real terminal surface; `--command` runs, `read-screen` and `send-text` all work. The earlier 2026-05-14 calibration note (which insisted `--focus true` was mandatory) is **OBSOLETE** — that build has since been fixed.

**Default rule:** ALL spawn calls pass `--focus false` (or omit the flag — that's the default). Only pass `--focus true` if the user EXPLICITLY says they want to switch to the new workspace, and only when invoking from a human-driven session. Background dispatchers (the Assistant agent, cron-driven spawns, watchdog respawns) MUST NOT steal focus.

If you find yourself wanting to restore origin focus after the fact via `cmux identify --json` + `.focused.workspace_ref`, that's a smell — `.focused` returns whatever tab Mukul is currently on, not the caller's own workspace, so the "restore" hops focus around unpredictably. Don't take focus, and you don't have to restore anything.

**CRITICAL — prefer `new-workspace --command '<text>'` over a separate `surface.send_text` burst for the `claude` launch.** The `--command` flag sends text + Enter atomically on creation, which avoids the burst-timing edge cases that caused the old skill to race the shell. Verified 2026-05-14 — `cmux new-workspace --focus true --cwd ... --command "echo HELLO"` → the terminal is created, the shell runs, and the echo output appears in `surface.read_text`. This replaces the `cmux rpc surface.send_text "claude ..."` step that used to live in Step 5. The prompt body still goes through the file-reference pattern below — only the `claude` launch command itself moves to `--command`.

**CRITICAL — param name is `surface_id`, not `surface`.** An earlier version of this skill used `{"surface": "surface:N", ...}` which cmux silently ignores (no error), causing every call to operate on the **currently-focused surface** instead of the requested one. That makes `send_text` type into the caller's own terminal and `read_text` return the caller's own screen — producing false-positive "Claude is ready" readings and clobbering the caller's input buffer. Always use `surface_id`. The value can be a ref (`surface:N`), UUID, or index.

**CRITICAL — `surface.send_text` does NOT use bracketed paste on this build AND drops chunks of long payloads.** The payload is streamed as individual keystrokes. Observed failure 2026-05-12: a ~7 KB prompt paste lost the middle ~60% of the content (only the first and last sections landed in Claude's input box). That is why this skill now never sends the prompt body via keystrokes — it writes the prompt to a persistent file on disk and sends only a short `Read <path> in full and execute it.` instruction. Other consequences of the streaming model that still matter:
- A trailing `\n` in the short instruction fires Enter = **submits immediately**. Strip the trailing `\n` and press Enter explicitly.
- Claude Code renders each line as it arrives — there is usually NO `[Pasted text #N +N lines]` marker. The marker only shows up when the terminal emulator (not cmux) announces bracketed paste — which this build does not. **Do not rely on `[Pasted text` for verification.**
- Short instructions (~100 bytes) ingest in <1 s; skip the long sleep you'd need for a full-prompt paste.

**CRITICAL — terminal-screen scraping is unreliable; don't use it as the primary signal.** A ~6-8 KB prompt fills the terminal scrollback well past any `read_text` window you pick; progress indicators use a rotating pool of hundreds of verbs (`Musing`, `Shimmering`, `Noodling`, `Pondering`, …) that grows every release. Both regex narrowness and window size have produced false-negative `submitted=0` readings even when the spawned agent was actively processing. The transcript-based check below is the correct approach — only fall back to `read_text` as a last-resort "did Claude even start" diagnostic when no transcript ever appears.

**CRITICAL — resolve the cwd via `realpath` before slugging.** On macOS, `/tmp` is a symlink to `/private/tmp`; Claude Code records the resolved path. `readlink` is not enough (only follows one hop). Use `python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))"` or `cd "$cwd" && pwd -P`. Skipping this puts the project directory at a slug that never receives transcripts and the verification loop times out.

**CRITICAL — first-ever spawn in a never-used cwd triggers the trust prompt.** Claude Code shows `Is this a project you trust? 1. Yes, I trust this folder / 2. No, exit` on first launch in a given directory, and **`--dangerously-skip-permissions` does NOT bypass it**. The transcript file does not appear until the user answers, so the skill's transcript-based readiness poll will time out silently. Detect the prompt on the terminal screen (match `1. Yes, I trust this folder`) and auto-send `1` + Enter before starting the readiness wait. Once answered, Claude Code remembers the decision for that cwd.

**How to detect submission — use the session transcript (not readiness).**

Claude Code writes sessions to JSONL transcripts at `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`. The slug is the absolute working directory with `/` replaced by `-` (e.g. `/Users/mukuls/dev/firefly-platform` → `-Users-mukuls-dev-firefly-platform`). **Verified empirically on Claude Code v2.1.122 (2026-04-28):**

- The transcript file is **NOT** created at session boot. An idle session with the banner up has zero on-disk presence; the session UUID in the footer exists only in memory.
- The transcript is created **the instant the first user prompt is submitted** — the line has `{"type":"user","message":{"role":"user","content":"…the prompt…"}}`.

That means we need two different signals:

| Signal | Source | Why |
|---|---|---|
| `CLAUDE_READY` (did claude start OK?) | terminal screen: match `Claude Code v` | Only screen-visible marker prior to submission. Version-stable banner. |
| `SUBMITTED` (did our prompt land?) | transcript diff: new `*.jsonl` with a `type=user` line containing the prompt's first-40-char signature | Definitive on-disk record. No verb-scraping. |

**Submission-detection algorithm:**

1. Before spawning, snapshot the set of existing `*.jsonl` files under the resolved project directory.
2. Check readiness via the terminal screen (single `Claude Code v` match).
3. Stream the prompt, press Enter.
4. Poll for a new `*.jsonl` file (or an mtime bump) containing a `type=user` line whose content includes the prompt's signature. 30-second budget.
5. If `CLAUDE_READY=0`, dump the screen (bad permission flag, missing npm dep, unknown trust-prompt variant).
6. If `CLAUDE_READY=1` but `SUBMITTED=0`, dump the screen (likely a second-level permission prompt the skill didn't recognize).

Working RPC methods (confirmed via `cmux capabilities`):

| Need | RPC call |
|---|---|
| Read terminal screen | `cmux rpc surface.read_text '{"surface_id":"surface:N","lines":40}'` → JSON with `.text` |
| Send text | `cmux rpc surface.send_text '{"surface_id":"surface:N","text":"…"}'` |
| Send a named key | `cmux rpc surface.send_key '{"surface_id":"surface:N","key":"enter"}'` |

Smoke test the targeting works before trusting it: `cmux rpc surface.send_text '{"surface_id":"surface:N","text":"echo PING\n"}'` followed by `cmux rpc surface.read_text '{"surface_id":"surface:N","lines":5}'` — you should see `PING` in the response `.text`. If you see content from your *own* terminal, you've hit the param-name bug.

The plain CLI still works for:

- `cmux ping`
- `cmux identify --json`
- `cmux new-workspace --cwd <path> --name "<title>"` ← no `--json`, returns `OK workspace:N` as plain text
- `cmux list-pane-surfaces --workspace workspace:N` (plain) or `cmux rpc surface.list '{}'` (richer JSON)
- `cmux focus-window --window <ref>` (may error with "Invalid window id" if the window is already focused — ignore with `|| true`)
- `cmux select-workspace --workspace workspace:N`

`cmux identify --json` shape is `{"focused":{"workspace_ref":"workspace:N","window_ref":"window:N", …}}` — NOT `{"workspace":{"id":…}}`.

## When to use

Trigger when the user asks any of:

- "spawn a new Claude in a workspace and give it this prompt …"
- "start a new Claude agent in cmux for …"
- "kick off a parallel / background Claude job …"
- "open a new workspace and have Claude do X"
- "run this in the background"

Skip if they want to run in the *current* workspace — use the `cmux` skill directly.

## Inputs

Required: **prompt**. Everything else has defaults:

- **Working directory** — defaults to `~/dev` (NOT `$PWD`). Mukul's Claude Code permissions are scoped to `~/dev/**`, `~/.claude/**`, `~/.architect/**` — spawning at `~` (which is `$PWD` when Mukul runs from his login shell) puts the cwd outside those allow-rules and quietly hits permission walls on any file write at the project root. The `--add-dir ~/.claude --add-dir ~/.architect` flags baked into Step 3 still let the spawn touch those subdirs, but the cwd itself must be inside one of the allowed roots. Verified 2026-05-22 — defaulting to `~/dev` eliminates the entire class of "why is my builder silently blocked" failures. Override by passing an explicit absolute path if the work genuinely lives elsewhere (e.g. an active worktree under `~/dev/<repo>/worktrees/...`). Pass absolute paths only.
- **Title** — first 40 chars of the prompt if not provided.
- **Send mode** — `auto` by default (press Enter after paste). Use `paste` only if the user said "let me review" or the prompt would trigger destructive shell actions.
- **Model** — set `MODEL=sonnet` for **routine / periodic work** (scanners, evaluators, renderers, batch tasks, anything rule-based), or `MODEL=opus` for **decision-making work** (architecture, design, code review, multi-step reasoning). Default is `opus` — when in doubt, prefer Opus. Mukul's policy 2026-05-22: "Choose Sonnet 1M for routine periodic work. For decision making, use Opus 1M." The skill maps `opus` → `claude-opus-4-7[1m]` and `sonnet` → `claude-sonnet-4-6[1m]` (Bedrock prefixes are added automatically when `CLAUDE_CODE_USE_BEDROCK=1`).

## Execution

All steps run from Bash. Do NOT invoke the `cmux` skill via the Skill tool.

### Step 1 — sanity check + capture origin focus

```bash
cmux ping >/dev/null
ORIGIN_CTX=$(cmux identify --json)
ORIGIN_WS_REF=$(printf '%s' "$ORIGIN_CTX" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["focused"]["workspace_ref"])')
ORIGIN_WIN_REF=$(printf '%s' "$ORIGIN_CTX" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["focused"]["window_ref"])')
```

### Step 2 — stage the prompt in a persistent file and plan to deliver by-reference

**Never stream the prompt body through `surface.send_text`.** cmux drops middle chunks on payloads above ~3-4 KB (confirmed 2026-05-12 on a ~7 KB prompt — only head+tail landed). Write the prompt to a stable on-disk path and send a short instruction telling the spawned Claude to `Read` that file. The file must persist long enough for the spawned Claude to read it AND for the user to debug / resume from it if something goes wrong — **do NOT delete `$PROMPT_FILE` in step 8**. The skill keeps prompts for 7 days; older ones are swept at the start of the next spawn.

```bash
# Persistent path — ~/.claude/spawn-prompts/ (not /tmp, which macOS scrubs on reboot
# and cleans aggressively). Include the timestamp so parallel spawns don't collide.
PROMPT_DIR="$HOME/.claude/spawn-prompts"
mkdir -p "$PROMPT_DIR"
# Sweep prompts older than 7 days. Bounded growth without sacrificing the
# ability to resume / debug a spawn in the days after it fired. User decision
# 2026-05-21 — previously the skill ran a detached `sleep 600 && rm`, which
# made the prompt vanish before the user could re-deliver it by hand.
find "$PROMPT_DIR" -maxdepth 1 -type f -name 'prompt-*.md' -mtime +7 -delete 2>/dev/null || true
STAMP=$(date +%Y%m%d-%H%M%S-%N 2>/dev/null || date +%Y%m%d-%H%M%S)
PROMPT_FILE="$PROMPT_DIR/prompt-$STAMP.md"
cat > "$PROMPT_FILE" <<'PROMPT_EOF'
<verbatim prompt text>
PROMPT_EOF
```

Quoted `'PROMPT_EOF'` sentinel — literal `$`, backticks, and `!` are preserved.

### Step 3 — create the workspace (focused, with claude launch baked in)

`new-workspace` has no `--json`. It returns a single line `OK workspace:N`. Pass the `claude` launch command via `--command` so the terminal surface materialises with claude already starting.

```bash
# Pin the 1M-context build of the chosen model — same family the user's
# interactive shell alias picks. cmux's --command string is NOT alias-expanded,
# so set the model explicitly. Bedrock IDs are prefixed `us.anthropic.…`; the
# Anthropic-API path uses the bare slug.
#
# MODEL selection (default: opus):
#   sonnet → claude-sonnet-4-6[1m]   periodic / routine / rule-based work
#   opus   → claude-opus-4-7[1m]     decision-making / architecture / reasoning
case "${MODEL:-opus}" in
  sonnet) MODEL_SLUG="claude-sonnet-4-6[1m]" ;;
  opus|*) MODEL_SLUG="claude-opus-4-7[1m]" ;;
esac
if [ "${CLAUDE_CODE_USE_BEDROCK:-}" = "1" ]; then
  MODEL_ID="us.anthropic.$MODEL_SLUG"
else
  MODEL_ID="$MODEL_SLUG"
fi
CLAUDE_CMD="claude --dangerously-skip-permissions --add-dir ~/dev --add-dir ~/.claude --add-dir ~/.architect --model \"$MODEL_ID\""
OUT=$(cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false --command "$CLAUDE_CMD")
# OUT is "OK workspace:13"
WS_REF=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -n1)
```

**Do not use `awk '{print $N}'`** to extract the ref — this SKILL.md gets surfaced to the agent via the Skill tool, which performs shell-style variable expansion on the markdown body. `$2` / `$NF` get substituted to empty (or some stray value) before the agent ever runs the command, and the resulting `awk '{print }'` pipes through unchanged text. Always use an anchored regex extractor (`grep -oE`, `sed -nE`, or `python3 -c`) that contains no `$N` tokens.

Notes:
- **Default to `--focus false`** (re-verified 2026-05-23). The earlier 2026-05-14 "zero panes without --focus true" claim is OBSOLETE — current cmux creates a fully-functional surface on the unfocused workspace and `read-screen` / `send-text` work normally. Background callers (Assistant agent, watchdog respawns) MUST pass `--focus false` to avoid stealing Mukul's foreground tab. Pass `--focus true` only when a human-driven session has explicitly asked to switch into the new workspace.
- **`--command "<claude-launch>"` replaces the old "send_text the launch command" step.** The flag sends text + Enter atomically on creation, so the shell spawns AND the claude launch fires without a separate `surface.send_text` burst. Verified 2026-05-14 — eliminates the "early writes get swallowed" race that killed the old Step 5. The claude launch still needs `--dangerously-skip-permissions` + `--add-dir` for unattended work.
- **Model is selectable via `MODEL` env var (default opus, 1M context)** so the spawned agent has the same context budget as the user's interactive shell. Pass `MODEL=sonnet` for routine/periodic work (Sonnet 4.6 1M); leave unset for Opus 4.7 1M on decision-making tasks. Bedrock IDs are prefixed `us.anthropic.…`; Anthropic-API IDs use the bare slug. cmux's `--command` does not expand shell aliases, so this branch encodes the equivalent of the alias. When you bump the model versions, update both the alias and this skill together.
- `--cwd` must be absolute.
- `--name` is the workspace title; keep under 40 chars.

### Step 4 — find the initial surface

The CLI output for `list-pane-surfaces` is:

```
* surface:54  archffp-bootstrap  [selected]
```

The leading `* ` is a selected-marker. Use `$2` to extract `surface:N`:

```bash
SURFACE_REF=$(cmux list-pane-surfaces --workspace "$WS_REF" | grep -oE 'surface:[0-9]+' | head -n1)
```

The CLI output looks like `* surface:54  archffp-bootstrap  [selected]` — `$NF` would pick up `[selected]` and `$2` looks tempting but is unreliable because of the skill-tool variable-expansion issue described under `new-workspace`. Pattern-match `surface:N` directly.

The first surface in a fresh workspace is always the initial terminal.

### Step 5 — wait for claude readiness (launch is already firing from Step 3 --command)

The `claude --dangerously-skip-permissions --add-dir …` command was already sent by Step 3's `--command` flag. **Do NOT issue another `send_text` with the launch command here** — that would type it a second time on top of the already-running shell and mangle both invocations.

All I/O targets the surface by ref via RPC — **the plain `cmux send` / `send-key` shortcuts return "Surface is not a terminal" even when it is** on the current cmux build. Always use `rpc`.

The three `--add-dir` paths baked into Step 3's `--command` (`~/dev`, `~/.claude`, `~/.architect`) cover 97.5% of observed out-of-cwd writes across ~400 sessions. Without them, cross-worktree or cross-repo writes prompt for approval even under `--dangerously-skip-permissions`, which silently stalls the headless run.

**Answer the trust prompt if it appears, then poll for the `Claude Code v` banner on the screen.** The transcript does NOT exist yet (Claude Code doesn't write the `.jsonl` until the first user prompt lands) — readiness is a screen check, submission is a transcript check.

```bash
# First-launch-in-cwd guard: the "Is this a project you trust?" prompt.
# --dangerously-skip-permissions does NOT bypass it. Answer with "1" if seen.
sleep 2
TRUST=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":40}))' "$SURFACE_REF")" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("surface_ref")==sys.argv[1]; print(d.get("text",""))' "$SURFACE_REF")
if printf '%s' "$TRUST" | grep -q '1\. Yes, I trust this folder'; then
  cmux rpc surface.send_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"text":"1"}))' "$SURFACE_REF")" >/dev/null
  cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
fi

CLAUDE_READY=0
for i in $(seq 1 30); do
  TEXT=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":40}))' "$SURFACE_REF")" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("surface_ref")==sys.argv[1]; print(d.get("text",""))' "$SURFACE_REF")
  if printf '%s' "$TEXT" | grep -qE 'Claude Code v'; then
    CLAUDE_READY=1
    break
  fi
  sleep 1
done
```

If `CLAUDE_READY=0` after 30s, Claude never started — dump the surface via `surface.read_text` as a diagnostic (typically shows npm/permission errors or an unknown trust-prompt variant). The banner line is the only version-stable readiness marker; it's cheap to scrape and not in the verb pool.

### Step 6 — deliver the prompt (by file reference, never by keystroke-streaming the body)

**Before sending anything, snapshot the project's existing transcripts.** We'll use the appearance of a new one to confirm the spawned Claude started, and the appearance of a user line in that transcript to confirm submission.

```bash
# Resolve symlinks first — on macOS, /tmp → /private/tmp; Claude Code uses the resolved path.
CWD_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")
CWD_SLUG=$(printf '%s' "$CWD_REAL" | sed 's|/|-|g')
PROJECT_DIR="$HOME/.claude/projects/$CWD_SLUG"
mkdir -p "$PROJECT_DIR"  # may not exist yet if this cwd has never been opened
# find is zsh-nullglob-safe; `ls "$PROJECT_DIR"/*.jsonl` aborts under `set -eu` when empty.
list_transcripts() { find "$PROJECT_DIR" -maxdepth 1 -type f -name '*.jsonl' 2>/dev/null | sort -u; }
BEFORE=$(list_transcripts)
```

Instead of streaming the prompt body (which cmux silently truncates above ~3-4 KB), send a short single-line instruction telling the spawned Claude to `Read` the file that step 2 wrote. **Strip the trailing `\n` first** — `surface.send_text` doesn't use bracketed paste on this build, so a trailing newline becomes an Enter keystroke and auto-submits mid-paste.

```bash
# Short instruction — reliably fits in a single keystroke burst. No truncation risk.
INSTRUCTION="Read $PROMPT_FILE in full and execute every instruction in it."
python3 - "$SURFACE_REF" "$INSTRUCTION" <<'PYEOF'
import json, subprocess, sys
surface, text = sys.argv[1], sys.argv[2]
subprocess.run(["cmux", "rpc", "surface.send_text", json.dumps({"surface_id": surface, "text": text.rstrip("\n")})], check=True)
PYEOF

# Short instructions ingest in <1 s; keep a small buffer before pressing Enter.
sleep 1

if [ "$AUTO_SEND" = "1" ]; then
  cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
fi
```

**Verify via the transcript.** Poll for up to 30 seconds for:
1. A new `*.jsonl` file under `$PROJECT_DIR` that wasn't in `$BEFORE` — means the spawned Claude started.
2. A line in that transcript with `type=user role=user` and non-empty content containing the prompt-file path — means our short "Read $PROMPT_FILE …" instruction landed and was submitted.

```bash
# Signature = the prompt-file path. That's what we actually sent via send_text,
# so it will appear verbatim in the transcript's user line. Do NOT use the body
# of the prompt file — the body is loaded by Claude via Read later, not submitted.
SIG=$(printf '%s' "$PROMPT_FILE" | head -c 60)

SESSION_FILE=""
SUBMITTED=0
for i in $(seq 1 30); do
  AFTER=$(list_transcripts)
  NEW=$(comm -13 <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | grep -v '^$' || true)
  if [ -n "$NEW" ]; then
    # Use the newest among any new files.
    SESSION_FILE=$(printf '%s\n' "$NEW" | xargs ls -t 2>/dev/null | head -n1)
    # Scan for a user prompt line carrying our signature.
    if python3 - "$SESSION_FILE" "$SIG" <<'PY'
import json, sys
path, sig = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if d.get("type") == "user" and msg and msg.get("role") == "user":
                c = msg.get("content", "")
                if isinstance(c, list):
                    c = " ".join(str(x.get("text","") if isinstance(x, dict) else x) for x in c)
                if sig and sig in str(c):
                    sys.exit(0)
    sys.exit(1)
except FileNotFoundError:
    sys.exit(1)
PY
    then
      SUBMITTED=1
      break
    fi
  fi
  sleep 1
done
```

If `SUBMITTED=0`, fall back to a terminal-screen dump (read 240 lines) and report the failure. Otherwise the transcript is the authoritative record — we know exactly what was submitted because it's in the file.

### Step 7 — return focus to origin

```bash
cmux focus-window --window "$ORIGIN_WIN_REF" >/dev/null 2>&1 || true  # may error if already focused
cmux select-workspace --workspace "$ORIGIN_WS_REF" >/dev/null
```

Verify with `cmux identify --json` — `.focused.workspace_ref` should equal `$ORIGIN_WS_REF`. If it doesn't, tell the user; don't silently strand them.

### Step 8 — report (DO NOT delete the prompt file)

```bash
# DO NOT rm "$PROMPT_FILE" — the spawned Claude needs to Read it AFTER this
# script returns, and the user may want to resume / debug from it later.
# Bounded growth is enforced by the 7-day `find … -mtime +7 -delete` sweep
# at the start of the NEXT spawn (Step 2). No per-spawn timer here.
echo "workspace=$WS_REF surface=$SURFACE_REF prompt_file=$PROMPT_FILE claude_ready=$CLAUDE_READY submitted=$SUBMITTED origin=$ORIGIN_WS_REF"
```

Report back in one line: workspace ref, surface, **prompt file path** (so the user can re-deliver by hand if something went wrong), whether banner was detected, whether submission was confirmed (transcript has the Read instruction), and confirmation of focus return.

## Failure handling

- **cmux not running** (`cmux ping` fails) — tell the user "cmux is not running; start /Applications/cmux.app and retry." Do NOT launch it.
- **Surface is not a terminal** via rpc — reproduce with `cmux tree --workspace "$WS_REF"` and show it to the user. This indicates the pane started as a browser or launcher surface rather than terminal.
- **Claude never becomes ready** — restore origin focus first, then dump 40 lines from `surface.read_text` and stop. Leave the workspace intact for debugging.
- **Paste verification failed** — dump the tail and stop. Do NOT send Enter.
- **Permission-prompt interception inside the spawned claude** — if the spawned claude pauses on a `Do you want to proceed?` prompt, that's its own session's permission flow. Don't touch it — the user decided whether to allow non-interactive; we leave it to them.

## One-shot convenience block

Single Bash paste, background-by-default, auto-send-by-default. Keep the heredoc sentinel quoted. **The prompt body lives on disk; only a short `Read <path>` instruction is streamed through cmux — never the full prompt** (cmux drops middle chunks on payloads > ~3-4 KB).

```bash
set -eu
# Default cwd is ~/dev — Mukul's permissions are scoped to ~/dev/**,
# ~/.claude/**, ~/.architect/**, so spawning at $PWD (often ~) hits the
# permission wall on any file write at the project root. Override only
# when the work genuinely lives elsewhere, and pass an absolute path.
CWD="${CWD:-$HOME/dev}"
TITLE="${TITLE:-claude task}"
AUTO_SEND="${AUTO_SEND:-1}"

cmux ping >/dev/null

ORIGIN_CTX=$(cmux identify --json)
ORIGIN_WS_REF=$(printf '%s' "$ORIGIN_CTX" | python3 -c 'import json,sys; print(json.load(sys.stdin)["focused"]["workspace_ref"])')
ORIGIN_WIN_REF=$(printf '%s' "$ORIGIN_CTX" | python3 -c 'import json,sys; print(json.load(sys.stdin)["focused"]["window_ref"])')

# Persistent path so the spawned Claude can Read the file AFTER this script
# returns AND so the user can resume / debug from it later. Do NOT use mktemp
# in /tmp — keep it under ~/.claude/spawn-prompts/.
PROMPT_DIR="$HOME/.claude/spawn-prompts"
mkdir -p "$PROMPT_DIR"
# Sweep prompts older than 7 days (user decision 2026-05-21 — replaces the
# old detached `sleep 600 && rm` that deleted prompts before the user could
# re-deliver them by hand).
find "$PROMPT_DIR" -maxdepth 1 -type f -name 'prompt-*.md' -mtime +7 -delete 2>/dev/null || true
STAMP=$(date +%Y%m%d-%H%M%S-%N 2>/dev/null || date +%Y%m%d-%H%M%S)
PROMPT_FILE="$PROMPT_DIR/prompt-$STAMP.md"
cat > "$PROMPT_FILE" <<'PROMPT_EOF'
<<<PROMPT>>>
PROMPT_EOF

# Resolve symlinks on the cwd — macOS /tmp → /private/tmp etc. Claude Code uses
# the resolved path for the project slug.
CWD_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")
CWD_SLUG=$(printf '%s' "$CWD_REAL" | sed 's|/|-|g')
PROJECT_DIR="$HOME/.claude/projects/$CWD_SLUG"
mkdir -p "$PROJECT_DIR"

# `find` is zsh-nullglob-safe; `ls "$PROJECT_DIR"/*.jsonl` aborts under `set -eu`
# when there are no matches.
list_transcripts() {
  find "$PROJECT_DIR" -maxdepth 1 -type f -name '*.jsonl' 2>/dev/null | sort -u
}
BEFORE=$(list_transcripts)

# --focus false (default) is the right choice — current cmux creates a real
# terminal surface without focus stealing. Background dispatchers MUST stay
# unfocused to avoid disturbing Mukul's foreground tab.
# --command runs the claude launch atomically on creation; Step 5 no longer needs
# to send_text + send_key the launch separately.
# Pin the 1M-context build of the chosen model. cmux's --command string is NOT
# alias-expanded, so set the model explicitly. MODEL=sonnet for periodic/routine
# work; default opus for decision-making. Bedrock IDs are prefixed
# `us.anthropic.…`, Anthropic-API IDs use the bare slug.
case "${MODEL:-opus}" in
  sonnet) MODEL_SLUG="claude-sonnet-4-6[1m]" ;;
  opus|*) MODEL_SLUG="claude-opus-4-7[1m]" ;;
esac
if [ "${CLAUDE_CODE_USE_BEDROCK:-}" = "1" ]; then
  MODEL_ID="us.anthropic.$MODEL_SLUG"
else
  MODEL_ID="$MODEL_SLUG"
fi
CLAUDE_CMD="claude --dangerously-skip-permissions --add-dir ~/dev --add-dir ~/.claude --add-dir ~/.architect --model \"$MODEL_ID\""
WS_REF=$(cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false --command "$CLAUDE_CMD" | grep -oE 'workspace:[0-9]+' | head -n1)
SURFACE_REF=$(cmux list-pane-surfaces --workspace "$WS_REF" | grep -oE 'surface:[0-9]+' | head -n1)

# First-launch-in-cwd gate: Claude Code shows a "Is this a project you trust?"
# prompt that --dangerously-skip-permissions does NOT bypass. Peek the screen
# briefly; if we see option 1, answer it. The decision is remembered per cwd
# so subsequent spawns skip straight to the banner.
sleep 2
TRUST_SCREEN=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":40}))' "$SURFACE_REF")" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("surface_ref")==sys.argv[1]; print(d.get("text",""))' "$SURFACE_REF")
if printf '%s' "$TRUST_SCREEN" | grep -q '1\. Yes, I trust this folder'; then
  cmux rpc surface.send_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"text":"1"}))' "$SURFACE_REF")" >/dev/null
  cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
fi

# Readiness = `Claude Code v` on the screen. The transcript file is NOT created
# until the first user prompt is submitted, so we can't use it here (verified on
# Claude Code v2.1.122, 2026-04-28).
CLAUDE_READY=0
for i in $(seq 1 30); do
  TEXT=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":40}))' "$SURFACE_REF")" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("surface_ref")==sys.argv[1]; print(d.get("text",""))' "$SURFACE_REF")
  if printf '%s' "$TEXT" | grep -qE 'Claude Code v'; then CLAUDE_READY=1; break; fi
  sleep 1
done

SUBMITTED=0
if [ "$CLAUDE_READY" = "1" ]; then
  # Send a short "Read <prompt-file>" instruction — never the prompt body.
  # cmux truncates long streams silently; a path + directive always fits in one burst.
  INSTRUCTION="Read $PROMPT_FILE in full and execute every instruction in it."
  python3 - "$SURFACE_REF" "$INSTRUCTION" <<'PYEOF'
import json, subprocess, sys
surface, text = sys.argv[1], sys.argv[2]
subprocess.run(["cmux", "rpc", "surface.send_text", json.dumps({"surface_id": surface, "text": text.rstrip("\n")})], check=True, capture_output=True)
PYEOF
  sleep 1

  if [ "$AUTO_SEND" = "1" ]; then
    cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
  fi

  # Definitive submission check: transcript user line contains the prompt-file path.
  SIG=$(printf '%s' "$PROMPT_FILE" | head -c 60)
  for i in $(seq 1 30); do
    if [ -n "$SESSION_FILE" ] && [ -f "$SESSION_FILE" ]; then
      if python3 - "$SESSION_FILE" "$SIG" <<'PY'
import json, sys
path, sig = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if d.get("type") == "user" and msg and msg.get("role") == "user":
                c = msg.get("content", "")
                if isinstance(c, list):
                    c = " ".join(str(x.get("text","") if isinstance(x, dict) else x) for x in c)
                if sig and sig in str(c):
                    sys.exit(0)
    sys.exit(1)
except FileNotFoundError:
    sys.exit(1)
PY
      then
        SUBMITTED=1
        break
      fi
    fi
    sleep 1
  done
fi

cmux focus-window --window "$ORIGIN_WIN_REF" >/dev/null 2>&1 || true
cmux select-workspace --workspace "$ORIGIN_WS_REF" >/dev/null

# DO NOT rm "$PROMPT_FILE" — the spawned Claude Reads it AFTER this script
# returns, and the user may need it to resume / debug. Old prompts are
# garbage-collected by the 7-day `find … -mtime +7 -delete` sweep at the
# start of the next spawn (above).
echo "workspace=$WS_REF surface=$SURFACE_REF prompt_file=$PROMPT_FILE claude_ready=$CLAUDE_READY submitted=$SUBMITTED session=$SESSION_FILE origin=$ORIGIN_WS_REF"
```

Substitute `<<<PROMPT>>>` with the verbatim prompt content. Set `CWD`, `TITLE`, and `AUTO_SEND=0` as needed.

## Guardrails

- **Default to `--focus false`** (re-verified 2026-05-23). Background dispatchers (Assistant agent, watchdog respawns, cron-driven spawns) MUST pass `--focus false` to avoid stealing Mukul's foreground tab. Current cmux creates a real terminal surface without focus, so `read-screen` / `send-text` all work fine. Pass `--focus true` ONLY when a human-driven session explicitly asked to switch into the new workspace.
- **Use `--command "<launch>"` to fire the claude launch atomically.** On current cmux, `new-workspace --command` sends text + Enter as part of workspace creation, avoiding the burst-timing race that caused old-skill `send_text` bursts to occasionally drop the launch command. Do NOT additionally `send_text` the claude launch in Step 5 — it would type the command a second time.
- **Always use RPC for surface I/O** on this cmux build. CLI shortcuts (`cmux send`, `cmux read-screen`, `cmux paste-buffer`) fail with "Surface is not a terminal" and will silently break the flow.
- **Focus restoration is mandatory.** Even on every failure path, return focus to `$ORIGIN_WS_REF` before reporting.
- **Verify via the session transcript, not the screen.** Claude Code writes `~/.claude/projects/<cwd-slug>/<uuid>.jsonl` the instant the session starts and appends a `type=user role=user` line the instant a prompt is submitted. Snapshot the existing transcripts before spawning; after, poll for a new file and a user line whose content includes the prompt's signature. Screen scraping verb pools ("Musing…", "Thinking…", etc.) is fragile — there are hundreds of verbs and the list grows every release. Transcripts are the source of truth.
- **Resolve the cwd via `realpath` before slugging.** On macOS, `/tmp` is a symlink to `/private/tmp`; Claude Code uses the resolved path for the project slug. Without `realpath`, the skill polls the wrong directory forever.
- **Use `find`, not `ls *.jsonl`**, to enumerate transcripts. In zsh with `set -eu`, an empty glob aborts the script. `find -maxdepth 1 -type f -name '*.jsonl'` returns nothing silently.
- **Answer the first-launch trust prompt**. A brand-new cwd shows `Is this a project you trust? 1. Yes / 2. No` and `--dangerously-skip-permissions` does NOT bypass it. Before polling for the transcript, read the screen; if you see `1. Yes, I trust this folder`, send `1` + Enter. The decision is cached per-cwd, so follow-up spawns skip straight to the banner.
- **Never stream the prompt body through `send_text`.** cmux silently drops middle chunks of payloads above ~3-4 KB (verified 2026-05-12: a 7 KB prompt lost ~60% of the body). Write the prompt to `~/.claude/spawn-prompts/prompt-<timestamp>.md` and send a short `Read <path> in full and execute every instruction in it.` one-liner as the only keystroke burst. The spawned Claude loads the body via the Read tool, which has no length limit.
- **Keep the prompt file on disk for 7 days.** The spawned Claude reads it AFTER the skill script returns, and the user often wants to resume / re-deliver / inspect what was sent. Per-spawn timed deletion is forbidden — it stranded users who tried to resume a spawn ~hours later (incident 2026-05-21, prompt vanished at the 10-minute mark). Cleanup happens only via the `find … -mtime +7 -delete` sweep at the start of the next spawn.
- **Strip the trailing `\n` from the short instruction** before `send_text`. On this build, `send_text` streams keystrokes; a trailing newline fires Enter mid-paste and auto-submits before verification. Send the short instruction, sleep ~1s for ingestion, then press Enter explicitly via `send_key`.
- **Auto-send is the default**, but switch to `AUTO_SEND=0` (no Enter) when the prompt would trigger destructive shell actions. Warn the user that the prompt is staged but not submitted.
- **Never reuse an existing workspace** — this skill always creates a new one. For targeting existing workspaces, use the `cmux` skill directly.
- **Always pass `--dangerously-skip-permissions`** when launching the spawned `claude`. The whole point of this skill is unattended background work — without the flag, every tool use prompts for approval and the agent stalls. User decision, recorded 2026-04-24. If you need to change this default, update the launch command in steps 5 and in the one-shot block.
- The prompt file is kept for 7 days; cleanup happens via a `find … -mtime +7 -delete` sweep at the start of the next spawn (step 2), not by a per-spawn timer.
