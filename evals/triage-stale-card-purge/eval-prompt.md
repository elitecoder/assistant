# Triage stale-card-purge eval — one-shot run

You are running in **eval mode** as a single non-interactive Triage pulse.

- The world snapshot is at `$EVAL_WORLD`, not `~/.claude/cache/world.json`.
- The PRIOR triage-state from the last pulse is at `$EVAL_PRIOR_STATE`, not `~/.claude/cache/triage-state.json`. Read this as if it were `~/.claude/cache/triage-state.json`.
- The output triage-state file is at `$EVAL_STATE_OUT`.

Every other rule from `~/.claude/spawn-prompts/prompt-triage-agent.md` applies. Pay special attention to **Step 2.5 — Re-validate carried-over awaiting cards**.

## Scenario under test

This is the **2026-05-23 stale-card incident pattern**:

- **Prior pulse (200)**: emitted a `triage:autodispatch-unset:bulk` card listing `td-501, td-502 all have autoDispatch=null`.
- **Between pulses**: Mukul ran a bulk-flip script setting both TODOs' `autoDispatch=true` in `~/.claude/assistant-todo.json`.
- **Current world.json (this pulse)**: shows td-501 and td-502 with `autoDispatch: true` and `dispatchedAt` empty. They are now eligible Bucket B candidates.

Without Step 2.5, Triage would re-emit the stale card and fail to dispatch the now-eligible TODOs. With Step 2.5, Triage drops the card and dispatches them.

## Your job

Run the Triage routine. The Step 2.5 re-validation MUST catch the stale autodispatch-unset card because all referenced TODOs (td-501, td-502) now have autoDispatch=true (no longer null).

Then proceed to Step 3 — both td-501 and td-502 are Bucket B (autoDispatch=true, dispatchedAt empty). Pre-dispatch in-flight check: world.live_sessions[] is empty so no in-flight match. Both should dispatch.

Write `actions_taken[]` and `awaiting_input[]` to `$EVAL_STATE_OUT`. **Do NOT actually spawn cmux workspaces** — this is eval mode, just record the dispatch INTENT in actions_taken with kind=`dispatch-intent` and key=`triage:dispatch:td-NNN`.

## Required behavior (eval will check this)

1. **The output `awaiting_input[]` MUST NOT contain a card with key matching `autodispatch-unset`** (the stale card must be dropped).
2. **The output `actions_taken[]` MUST contain at least one entry with key matching `triage:awaiting-purged:triage:autodispatch-unset:bulk`** (or substring `awaiting-purged`) with `evidence` quoting the now-current autoDispatch=true state.
3. **The output `actions_taken[]` MUST contain dispatch entries for BOTH td-501 and td-502** (key matches `triage:dispatch:td-501` and `triage:dispatch:td-502`). Use `kind: "dispatch-intent"` since you can't actually spawn in eval mode.

## Format (write to $EVAL_STATE_OUT)

```json
{
  "_meta": {
    "generated_at": "<UTC ISO>",
    "model": "<your model id>",
    "pulse_idx": 201,
    "eval_mode": true
  },
  "actions_taken": [
    {
      "key": "triage:awaiting-purged:triage:autodispatch-unset:bulk",
      "kind": "awaiting-purged",
      "evidence": "<quote current autoDispatch=true state of td-501/td-502>"
    },
    {
      "key": "triage:dispatch:td-501",
      "kind": "dispatch-intent",
      "target": {"type": "todo", "ref": "td-501", "name": "..."}
    },
    {
      "key": "triage:dispatch:td-502",
      "kind": "dispatch-intent",
      "target": {"type": "todo", "ref": "td-502", "name": "..."}
    }
  ],
  "awaiting_input": []
}
```

When done, print on stdout exactly: `EVAL_PULSE_DONE` and exit. No other commentary.
