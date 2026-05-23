# Assistant dispatch-validation eval — one-shot run

You are running in **eval mode** as a single non-interactive Assistant pulse.

- World snapshot at `$EVAL_WORLD` (not `~/.claude/cache/world.json`).
- Output state file at `$EVAL_STATE_OUT`.

Re-read `~/.claude/spawn-prompts/prompt-assistant-agent.md`, especially the **MANDATORY post-spawn validation** section (Step 3, Bucket B / spawn pattern) and the **Re-validating older dispatches** sub-section.

## Scenario under test

The fixture contains:

- **One TODO** `td-901`: dispatched 30 minutes ago at 17:30Z, `dispatchedWs: workspace:9700`.
- **One live session** at workspace:9700 whose `last_user.text` is the literal `"Read /Users/mukuls/.claude/spawn-prompts/prompt-dispatch-td-901-... in full and execute every instruction in it."` AND `last_assistant: null` AND `last_turn_age_sec: 1800`.

This is the **stranded-dispatch pattern** from the 2026-05-23 ws:28/ws:29 incident: the prompt was typed into the input box but never submitted, so the spawned Claude has zero assistant turns 30 minutes later.

## Your job

You MUST detect this as a stranded dispatch and act on it. Acceptable outcomes (any one of these passes):

**Outcome A — flag + surface for rescue (preferred for eval mode, since you can't actually `cmux send-key` here):**
1. Add `actions_taken[]` entry with key matching `assistant:dispatch-failed:td-901` OR `assistant:dispatch-stranded:td-901` OR `assistant:needs-rescue:td-901`. The substring `dispatch-failed` / `stranded` / `needs-rescue` is what the eval looks for.
2. Add `awaiting_input[]` card pointing at the workspace, asking Mukul to rescue or instructing what to do.

**Outcome B — auto-rescue logged (also passes):**
1. Log a `assistant:dispatch-rescued:td-901` action with evidence describing the rescue attempt (would-have-sent-Return / would-have-re-pasted).

**Either way, you MUST NOT:**
- Log a `assistant:dispatch:td-901` action as if it succeeded (it didn't — last_assistant is null).
- Move the TODO's `dispatchedAt` forward as if everything is fine.
- Spawn a duplicate workspace for td-901 (the in-flight check should catch that — `workspace:9700` IS the dispatched workspace, just stuck).

## Required behavior (eval will check this)

1. **Output `actions_taken[]` MUST contain at least one entry** whose key contains `dispatch-failed`, `dispatch-stranded`, `dispatch-rescued`, or `needs-rescue` AND references `td-901`.
2. **Output `actions_taken[]` MUST NOT contain any entry** whose key matches `assistant:dispatch:td-901` (the success-path key — would mean the agent treated this as a normal successful dispatch).
3. **Evidence in the failure/rescue entry** MUST quote the stranded condition: either the literal prompt text in the user message, OR the `last_assistant: null` / `last_turn_age_sec` observation, OR the workspace ref `workspace:9700`.

## Format (write to $EVAL_STATE_OUT)

```json
{
  "_meta": {
    "generated_at": "<UTC ISO>",
    "model": "<your model id>",
    "pulse_idx": 1,
    "eval_mode": true
  },
  "actions_taken": [
    {
      "key": "assistant:dispatch-failed:td-901",
      "kind": "dispatch-failed",
      "target": {"type": "todo", "ref": "td-901"},
      "evidence": "<quote stranded condition>",
      "verified": true
    }
  ],
  "awaiting_input": [
    {"key": "assistant:needs-you:dispatch-failed:td-901", "tier": "T2", "title": "...", "detail": "..."}
  ]
}
```

When done, print exactly: `EVAL_PULSE_DONE` and exit.
