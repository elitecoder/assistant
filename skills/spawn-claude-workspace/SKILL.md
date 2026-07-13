---
name: spawn-claude-workspace
description: Spawn a new unfocused cmux workspace, start Factory Droid on GLM-5.2 with the user's current permissions and instructions, and deliver a prompt by file reference. Use for parallel or background coding work. The legacy skill name is retained for command compatibility.
---

# Spawn Factory Droid workspace

Spawn a Factory Droid session in a new cmux workspace without taking focus. Use the `cmux-workspace` skill's targeting rules: always target explicit workspace/surface refs, use RPC parameter `surface_id`, and never call focus-changing commands.

## Invariants

- Always pass `--focus false` to `cmux new-workspace`.
- Launch Droid with `~/.assistant/droid-glm-settings.json`, `--auto high`, and `~/.claude/CLAUDE.md`. This matches the current Claude permission posture while fixing the model at GLM-5.2.
- Never stream a long prompt through cmux. Stage it under `~/.assistant/spawn-prompts/` and send a short `Read <path>...` instruction.
- Use `surface.send_text` followed by an explicit `surface.send_key` Enter.
- Treat the Factory transcript as the authoritative submission signal. Droid transcripts are under `~/.factory/sessions/<real-cwd-slug>/<session-id>.jsonl`; user turns use `{"type":"message","message":{"role":"user",...}}`.
- Never launch `claude`, and never fall back to Claude if Droid fails.

## Inputs

- Prompt: required, preserved verbatim in the staged file.
- Working directory: explicit absolute path, default `~/dev`.
- Title: concise, at most 40 characters.
- Send mode: `auto` unless the user explicitly asks to review before submission.

## Procedure

1. Verify cmux and required Droid configuration:

```bash
cmux ping >/dev/null
test -f "$HOME/.assistant/droid-glm-settings.json"
```

2. Resolve the cwd physically and derive the Factory transcript directory:

```bash
CWD="${CWD:-$HOME/dev}"
CWD=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CWD")
SLUG=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]).replace("/", "-"))' "$CWD")
SESSION_DIR="$HOME/.factory/sessions/$SLUG"
mkdir -p "$SESSION_DIR" "$HOME/.assistant/spawn-prompts"
```

3. Stage the complete prompt in a unique Markdown file. Use the native file-writing tool so the content is literal and no shell interpolation occurs:

```text
~/.assistant/spawn-prompts/prompt-<timestamp>.md
```

4. Snapshot existing transcripts, then create the unfocused workspace with Droid already launching:

```bash
BEFORE=$(mktemp)
find "$SESSION_DIR" -maxdepth 2 -type f -name '*.jsonl' -print 2>/dev/null | sort > "$BEFORE"
DROID_CMD="droid --settings '$HOME/.assistant/droid-glm-settings.json' --auto high"
if test -f "$HOME/.claude/CLAUDE.md"; then
  DROID_CMD="$DROID_CMD --append-system-prompt-file '$HOME/.claude/CLAUDE.md'"
fi
OUT=$(cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false --command "$DROID_CMD")
WS_REF=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -n1)
SURFACE_REF=$(cmux list-pane-surfaces --workspace "$WS_REF" | grep -oE 'surface:[0-9]+' | head -n1)
test -n "$WS_REF" && test -n "$SURFACE_REF"
```

5. Poll the whole screen until Droid is ready. Read 200 lines and accept any observed Droid marker: `GLM-5.2`, `allow all commands`, `Skills (<n>)`, or `? for help`. Do not accept a Claude banner.

```bash
READY=0
for _ in $(seq 1 30); do
  SCREEN=$(cmux rpc surface.read_text "$(python3 -c 'import json,sys; print(json.dumps({"surface_id":sys.argv[1],"lines":200}))' "$SURFACE_REF")" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("text", ""))')
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

7. In `auto` mode, confirm submission for up to 30 seconds. A new or changed Factory transcript must contain a Droid user-message whose text includes the staged prompt path. A cmux success response alone is not confirmation. If confirmation fails, leave the workspace and prompt file intact, report the workspace ref, and do not launch Claude or spawn a duplicate.

8. Report the workspace ref, surface ref, staged prompt path, and whether transcript confirmation succeeded. Never select the new workspace automatically.
