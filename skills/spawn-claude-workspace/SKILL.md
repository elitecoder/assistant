---
name: spawn-claude-workspace
description: "Spawn an unfocused cmux workspace running an autonomous coding agent, delivering a prompt by file reference for background work. `engine` picks the agent: `droid` (default), `claude`, or `copilot`. Use `engine=claude` for Claude-plugin work like archffp."
---

# Spawn autonomous coding workspace

Spawn an autonomous coding agent in a new cmux workspace without taking focus. Use the `cmux-workspace` skill's targeting rules: always target explicit workspace/surface refs, use RPC parameter `surface_id`, and never call focus-changing commands.

Every engine-specific detail (launch command, readiness marker, trust prompt, transcript location) comes from Assistant's one agent seam, `bin/agent_session.py`. Never hand-write a `claude`, `copilot`, or `droid` command line here.

## Invariants (every engine)

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

## Inputs

- **Engine**: `droid` (default), `claude`, or `copilot`. Use `claude` for Claude Code plugin work such as `/architect-ffp:archffp`. Never fall back to another engine when the chosen one fails.
- **Prompt**: required, preserved verbatim in the staged file.
- **Working directory**: explicit absolute path, default `~/dev`.
- **Title**: concise, at most 40 characters.
- **Send mode**: `auto` unless the user explicitly asks to review before submission.

## Procedure

1. Locate the seam, verify cmux, and check the engine. If the engine isn't installed, stop and report it; don't launch another engine.

```bash
SEAM="$(python3 -c 'import os; print(os.path.realpath(os.path.expanduser("~/.claude/skills/spawn-claude-workspace")))')/../../bin/agent_session.py"
cmux ping >/dev/null
ENGINE="${ENGINE:-droid}"
python3 "$SEAM" agent --agent "$ENGINE"
SPAWNED_AT=$(date +%s)
CWD="${CWD:-$HOME/dev}"
CWD=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")
mkdir -p "$HOME/.assistant/spawn-prompts"
```

2. Stage the complete prompt in a unique Markdown file. Use the native file-writing tool so the content is literal and no shell interpolation occurs:

```text
~/.assistant/spawn-prompts/prompt-<timestamp>.md
```

3. Create the unfocused workspace with the engine already launching:

```bash
LAUNCH_CMD=$(python3 "$SEAM" launch-command --agent "$ENGINE" --unattended)
OUT=$(cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false --command "$LAUNCH_CMD")
WS_REF=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -n1)
SURFACE_REF=$(cmux list-pane-surfaces --workspace "$WS_REF" | grep -oE 'surface:[0-9]+' | head -n1)
test -n "$WS_REF" && test -n "$SURFACE_REF"
```

4. Poll the screen until the engine is ready, answering the first-launch folder-trust prompt once with the seam's lasting-yes key + Enter. An unanswered trust prompt outranks a ready marker on the same screen:

```bash
TRUST=$(python3 "$SEAM" trust-marker --agent "$ENGINE")
TRUST_ANSWER=$(python3 "$SEAM" trust-answer --agent "$ENGINE")
READY=0; TRUSTED=0
for _ in $(seq 1 60); do
  SCREEN=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":200}))' "$SURFACE_REF")" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("text", ""))')
  if [ "$TRUSTED" = 0 ] && [ -n "$TRUST" ] && printf '%s' "$SCREEN" | grep -qF "$TRUST"; then
    cmux rpc surface.send_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"text":sys.argv[2]}))' "$SURFACE_REF" "$TRUST_ANSWER")" >/dev/null
    cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
    TRUSTED=1
  elif printf '%s' "$SCREEN" | python3 "$SEAM" is-ready --agent "$ENGINE"; then READY=1; break
  fi
  sleep 1
done
test "$READY" = 1
```

5. Deliver only the short file-reference instruction:

```bash
INSTRUCTION="Read $PROMPT_FILE in full and execute every instruction in it."
cmux rpc surface.send_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"text":sys.argv[2]}))' "$SURFACE_REF" "$INSTRUCTION")" >/dev/null
cmux rpc surface.send_key "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"key":"enter"}))' "$SURFACE_REF")" >/dev/null
```

If send mode is `paste`, omit the Enter call and report that the prompt is staged but not submitted.

6. In `auto` mode, confirm submission for up to 30 seconds. The engine's transcript must record a user turn that contains the staged prompt path. A cmux success response alone is not confirmation. If confirmation fails, leave the workspace and prompt file intact, report the ref, and don't launch another engine or spawn a duplicate.

```bash
for _ in $(seq 1 30); do
  python3 "$SEAM" submitted --agent "$ENGINE" --cwd "$CWD" --needle "$PROMPT_FILE" --since "$SPAWNED_AT" && break
  sleep 1
done
```

7. Report the engine, workspace ref, surface ref, staged prompt path, and whether transcript confirmation succeeded. Never select the new workspace automatically.

## Post-spawn verification (unattended)

Don't trust `ps` alone — a startup-blocked TUI sits alive doing nothing. Verify the `submitted` check passes within about 2 minutes, then verify downstream artifacts (worktree, branch, PR) on the first check-in.
