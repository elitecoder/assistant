# 13 — one-shot diagnostic fully answered → NOT stranded (REAL ws:89)

The sibling misfire to fixture 12, captured live in the same pulses on
2026-05-30. Where ws:90 (fixture 12) got a silent `active` miss, ws:89
got the worse end of the same gap: an outright **`stranded` nudge that
told a finished workspace to "Please resume or wrap up."** That nudge is
the exact behavior the operator was correcting.

## Provenance — real transcript

- **Workspace:** ws:89, `td-099: Replicate Auto-pilot knowledge-cutoff`.
- **Transcript:** the complete real session
  `~/.claude/projects/-Users-mukuls-dev-firefly-platform/8518e61b-bc1c-47ca-851d-5f705bbe2591.jsonl`
  (1 user turn + 1 assistant turn). The task was a one-shot diagnostic:
  *"Read time-ruler-element.ts, then list every absolute path of a
  .claude/rules/**.md file currently loaded in your context. Output ONLY
  the paths."* The agent answered completely — a clean list of 14 rule
  paths — and stopped. Nothing is left to do.
- **ctx.json:** the real values Observer saw at run 0445 —
  `last_turn_age_sec: 13576` (3.8h), `agent_status: idle`,
  `cwd_dirty: false`, `cwd_unpushed: false`.

## The real misfire this pins against

Observer's verdict for ws:89 flip-flopped across consecutive pulses
(`~/.assistant/observer-runs/`):

| run  | verdict             | note |
|------|---------------------|------|
| 0441 | `ready_for_cleanup` | |
| 0442 | `ready_for_cleanup` | |
| 0443 | `active`            | silent miss |
| 0444 | `active`            | silent miss |
| 0445 | **`stranded`**      | sent nudge: *"…Please resume or wrap up."* ← the bug |
| 0446 | `ready_for_cleanup` | |

The `stranded` call at 0445 is the failure this fixture guards. The
agent did **not** trail off mid-step — it fully answered a one-shot
question. Old rule B3 only required "idle >30min + idle + last text is
mid-narrative, NOT a recap," and the model misread a bare factual
answer (a list of paths, no closing sentence) as "mid-narrative," then
nudged a done workspace to keep going.

## What Observer must do now

The tightened B3 (stranded) now requires the last text to be **truly
mid-step** — "trailed off inside a step with no handoff to the user" —
and explicitly excludes recaps and completed answers. A fully-answered
one-shot diagnostic with a clean cwd and no open iteration is
`ready_for_cleanup` (the deliverable was the answer; nothing remains to
review).

This fixture is pinned to the **positive** verdict the model now
produces deterministically (`ready_for_cleanup`, observed identical
across repeated runs) AND to the hard guarantee that matters most:
`stranded` is FORBIDDEN. The operator's correction was "stop telling
finished workspaces to resume" — that is what
`forbidden_verdicts: ["stranded"]` enforces.

Note: cleanup here is NOT auto-fired — ws:89 has no Assistant-merge
record, so `bin/pulse.py` downgrades `ready_for_cleanup` to a
confirm-card rather than sending `/cleanup`. The operator confirms
teardown; the system never discards the workspace on its own.

Expected: `ready_for_cleanup` (forbidden: `stranded`).
