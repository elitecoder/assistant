# Assistant in-flight check eval — one-shot run

You are running in **eval mode** as a single non-interactive Assistant pulse. Treat this run as identical to a real Assistant pulse, with two differences:

1. The world snapshot is at `$EVAL_WORLD` (an env var pointing to a fixture file), not `~/.claude/cache/world.json`.
2. The output assistant-state file is at `$EVAL_STATE_OUT` (an env var), not `~/.claude/cache/assistant-state.json`.

Every other rule from `~/.claude/spawn-prompts/prompt-assistant-agent.md` applies — re-read that file now to refresh your memory, especially the **Pre-dispatch in-flight check** section.

## Scenario under test

The fixture contains:

- **One open TODO**: `td-019` — "Deferrals worktree ... contains can-expand-trim feature." `autoDispatch: true`, `dispatchedAt` is empty (Bucket B — would normally fire an initial dispatch).
- **One live session**: `workspace:9898` titled "ship td-019 can-expand-trim", whose recent_turns clearly show it is actively shipping the same work (PR #10164 opened ~15 min ago, CI monitor running).

This is the **td-019 incident pattern (2026-05-22)**: the TODO's `dispatchedWs` is stale (empty), but a workspace IS already in flight doing the same work. Without the in-flight check, Bucket B would dispatch a duplicate.

## Your job

Follow the Assistant routine. The **Pre-dispatch in-flight check** must catch the overlap:
- The live session's title contains `td-019` literally (full match).
- It also contains the distinctive token `can-expand-trim`.
- Its cwd matches the worktree path mentioned in the TODO's `source` field.

Multiple match signals — this is a clear duplicate.

Decide what to do, then write `actions_taken[]` and `awaiting_input[]` arrays to `$EVAL_STATE_OUT` in the same JSON format as the real `assistant-state.json`.

Do NOT execute any cmux commands. This is eval mode. Just decide and write the state file.

## Required behavior (the eval will check this)

1. **You MUST NOT spawn a new workspace.** No `assistant:dispatch:td-019` action in `actions_taken[]`. No `cmux new-workspace`-related action.
2. **You MUST add an `actions_taken[]` entry with key matching `assistant:dispatch-skipped:td-019:already-in-flight`** (the substring `dispatch-skipped` is the load-bearing assertion).
3. **The action's `evidence` field MUST quote** the matched workspace's title or ws_ref or the matching keyword (so we can prove the agent actually ran the check, not just guessed).
4. **You MAY update the TODO's `dispatchedWs`** to point at `workspace:9898` and `dispatchedAt` to its `ts` from world.json. (Not strictly required for this eval to pass, but matches the prompt's directive.)
5. **You MAY surface a confirm-binding card** (`assistant:dispatch-skipped:td-019:confirm-binding`) — also not strictly required.

## Format (write to $EVAL_STATE_OUT)

```json
{
  "_meta": {
    "generated_at": "<UTC ISO>",
    "model": "<your model id>",
    "pulse_idx": 1,
    "n_sessions_reviewed": 1,
    "n_actions_taken": 1,
    "n_awaiting": 0,
    "eval_mode": true
  },
  "actions_taken": [
    {
      "ts": "<UTC ISO>",
      "key": "assistant:dispatch-skipped:td-019:already-in-flight",
      "kind": "dispatch-skipped",
      "target": {"type": "todo", "ref": "td-019", "name": "..."},
      "evidence": "<quote workspace:9898 title or matched keyword>",
      "verified": true
    }
  ],
  "awaiting_input": []
}
```

When done, print on stdout exactly: `EVAL_PULSE_DONE` and exit. No other commentary.
