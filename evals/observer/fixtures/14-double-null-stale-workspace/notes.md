# Fixture 14 — double-null stale workspace

## Scenario

A workspace has been alive for 3 hours (`last_turn_age_sec: 10800`) with:
- `transcript_path: null` — no readable transcript found
- `screen_text: ""` — cmux failed to read the screen (workspace missing from cmux, or surface not found)
- `cwd_dirty: true`, `cwd_unpushed: true` — uncommitted/unpushed work exists on disk

## Why this is `needs_user`, not `active`

The combination of:
1. No transcript visible to the Observer
2. No screen readable by cmux
3. 3 hours of age
4. Uncommitted changes on disk

...strongly implies the workspace is stuck — likely waiting on user input (e.g. an AskUserQuestion dialog) that the Observer cannot see because cmux screen read failed. Defaulting to `active` in this state leaves the workspace silently blocked indefinitely.

The correct verdict is `needs_user` with a message asking Mukul to inspect the workspace manually.

## What the Observer should NOT do

- Return `active` — there is zero evidence of forward progress, and 3 hours have elapsed.
- Return `stranded` — that implies the agent is stuck mid-task with recoverable context. Here we can't even tell.

## The bug this fixture guards against

On 2026-06-07, workspace:105 (archffp meta-gate build) displayed an `AskUserQuestion` dialog for 3+ hours. The Observer classified it `active` every pulse because both transcript_path and screen_text were null/empty. Mukul only found it by manually asking for an update.
