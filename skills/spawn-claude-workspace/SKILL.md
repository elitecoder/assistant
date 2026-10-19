---
name: spawn-claude-workspace
description: Spawn an unfocused cmux workspace running an autonomous coding agent, delivering a prompt by file reference for background work. `engine` picks the agent (default `droid`); use `engine=claude` for Claude-plugin work like archffp.
---

# Spawn autonomous coding workspace

Spawn an autonomous coding agent in a new cmux workspace without taking focus. Use the `cmux-workspace` skill's targeting rules: always target explicit workspace/surface refs, use RPC parameter `surface_id`, and never call focus-changing commands.

Two engines are supported, selected by the `engine` input:

- **`droid`** (default) — Factory Droid on GLM-5.2. The historical behavior; unchanged.
- **`claude`** — Claude Code via the login-shell `claude` alias. Use when the task requires a Claude-only capability, most importantly a Claude Code plugin skill such as `/architect-ffp:archffp` (Droid cannot run Claude plugins).

## Invariants (both engines)

- Always pass `--focus false` to `cmux new-workspace`.
- Never stream a long prompt through cmux. Stage it under `~/.assistant/spawn-prompts/` and send a short `Read <path>...` instruction.
- Use `surface.send_text` followed by an explicit `surface.send_key` Enter.
- Never select the new workspace automatically.
- The transcript is the authoritative submission signal — a cmux success response alone is NOT confirmation.

## Workspace name suffix and sidebar color — NOT owned here

Do not set a `[N]` name suffix or a sidebar color in this skill. The
`cmux-ws-numberer` daemon (`bin/cmux-ws-numberer.py`, a launchd job) owns both:
it polls every ~3s and appends a `[N]` suffix plus a distinct palette color to
every workspace that lacks one. `N` is the workspace's 0-based sidebar position,
which `--workspace <index>` accepts. It isn't the `workspace:N` ref, and it
changes when workspaces move or close. Target workspaces by ref, never by the
bracket number. Leave naming and color to the daemon.

## Engine-specific launch

### `droid` (default)

- Launch Droid with `~/.assistant/droid-glm-settings.json`, `--auto high`, and `~/.claude/CLAUDE.md`. This matches the current Claude permission posture while fixing the model at GLM-5.2.
- Droid transcripts: `~/.factory/sessions/<real-cwd-slug>/<session-id>.jsonl`; user turns use `{"type":"message","message":{"role":"user",...}}`.
- Never fall back to Claude if Droid fails.

### `claude`

- Launch with `/Users/mukuls/.claude/bin/cmux-claude-spawn.sh`. It starts the login-shell `claude` alias, which runs in auto mode, so every child session is checked by the same auto-mode classifier. Never add `--dangerously-skip-permissions`; the script refuses to launch when the alias carries it or isn't auto mode.
- Run the script by that exact absolute path, as one command with nothing piped, chained, or captured around it, so its `permissions.allow` rule applies. Give it a 5-minute timeout.
- To message a running child session, stage a file under `~/.assistant/spawn-prompts/` and run `/Users/mukuls/.claude/bin/cmux-claude-send.sh <surface-uuid> <transcript> <prompt-file>`, the same way.
- Do NOT hardcode `--model`/`--add-dir` — the alias owns them. Override `--model` only when the task genuinely needs a different tier.
- Claude transcripts: `~/.claude/projects/<project-slug>/<session-id>.jsonl`. `<project-slug>` is the cwd with every `/` replaced by `-` (leading `-` included). Sessions share one project dir; confirmation is a NEW jsonl appearing after spawn.
- Never fall back to Droid if Claude fails.

## Inputs

- **Engine**: `droid` (default) or `claude`.
- **Prompt**: required, preserved verbatim in the staged file.
- **Working directory**: explicit absolute path, default `~/dev`.
- **Title**: concise, at most 40 characters.
- **Send mode**: `auto` unless the user explicitly asks to review before submission.

## Procedure

For `engine=claude`, do step 3, then run `/Users/mukuls/.claude/bin/cmux-claude-spawn.sh [--env FFP_CHANNEL=<v>|FFP_EXP_<ID>=<v>]... <cwd> <title> <prompt-file>` in place of steps 1, 2, and 4-7. It prints `<workspace-ref> <surface-uuid> <transcript>` and exits 0 only when the transcript shows the instruction. On a nonzero exit, leave the workspace and prompt file intact, report the refs it printed, and don't spawn a duplicate. The steps below are for `engine=droid`.

1. Verify cmux and the engine's prerequisites:

```bash
cmux ping >/dev/null
# engine=droid:
test -f "$HOME/.assistant/droid-glm-settings.json"
```

2. Resolve the cwd physically and derive the engine's transcript directory:

```bash
CWD="${CWD:-$HOME/dev}"
CWD=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")
# engine=droid:
SLUG=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]).replace("/", "-"))' "$CWD")
SESSION_DIR="$HOME/.factory/sessions/$SLUG"
mkdir -p "$SESSION_DIR" "$HOME/.assistant/spawn-prompts"
```

3. Stage the complete prompt in a unique Markdown file. Use the native file-writing tool so the content is literal and no shell interpolation occurs:

```text
~/.assistant/spawn-prompts/prompt-<timestamp>.md
```

4. Snapshot existing transcripts, then create the unfocused workspace with the engine already launching:

```bash
BEFORE=$(mktemp)
find "$SESSION_DIR" -maxdepth 2 -type f -name '*.jsonl' -print 2>/dev/null | sort > "$BEFORE"
# engine=droid:
LAUNCH_CMD="droid --settings '$HOME/.assistant/droid-glm-settings.json' --auto high"
if test -f "$HOME/.claude/CLAUDE.md"; then
  LAUNCH_CMD="$LAUNCH_CMD --append-system-prompt-file '$HOME/.claude/CLAUDE.md'"
fi
OUT=$(cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false --command "$LAUNCH_CMD")
WS_REF=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -n1)
SURFACE_REF=$(cmux list-pane-surfaces --workspace "$WS_REF" | grep -oE 'surface:[0-9]+' | head -n1)
test -n "$WS_REF" && test -n "$SURFACE_REF"
```

5. Poll the whole screen until the engine is ready. Read 200 lines and accept an engine-appropriate marker:

- **droid**: `GLM-5.2`, `allow all commands`, `Skills (<n>)`, or `? for help`. Do not accept a Claude banner.

```bash
READY=0
for _ in $(seq 1 30); do
  SCREEN=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":200}))' "$SURFACE_REF")" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("text", ""))')
  # engine=droid:
  if printf '%s' "$SCREEN" | grep -Eq 'GLM-?5\.2|allow all commands|Skills \([0-9]+\)|\? for help'; then READY=1; break; fi
  sleep 1
done
test "$READY" = 1
```

6. Deliver only the short file-reference instruction:

```bash
INSTRUCTION="Read $PROMPT_FILE in full and execute every instruction in it."
cmux rpc surface.send_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"text":sys.argv[2]}))' "$SURFACE_REF" "$INSTRUCTION")" >/dev/null
cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
```

If send mode is `paste`, omit the Enter call and report that the prompt is staged but not submitted.

7. In `auto` mode, confirm submission for up to 30 seconds. A new or changed transcript under `SESSION_DIR` (compare against `BEFORE`) must contain a user-message whose text includes the staged prompt path. A cmux success response alone is not confirmation. If confirmation fails, leave the workspace and prompt file intact, report the ref, and do not launch the other engine or spawn a duplicate.

8. Report the engine, workspace ref, surface ref, staged prompt path, and whether transcript confirmation succeeded. Never select the new workspace automatically.

## Post-spawn verification (claude, unattended)

For `engine=claude` background dispatches, do not trust `ps` alone — a startup-blocked TUI sits alive doing nothing. Verify a new `~/.claude/projects/<CSLUG>/*.jsonl` appears within ~2 min AND keeps growing. Then verify downstream artifacts (archffp worktree/branch/PR) on the first check-in.
