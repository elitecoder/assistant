# 12 — work delivered, awaiting human review → needs_user

This is the missed-state class the operator reported on 2026-05-30:
**work is complete, but a human must review it and give next steps.**
Not stalled — finished and correctly waiting. Grounded in the real
td-098 transcript (ws:90) where the agent ran a coalesce/undo
granularity audit, produced an audit doc + two draft enforcement-rule
backstops, and signed off with "ready for your review … say the word
and I'll wire it in." Over 20 transcripts in the trailing 2 days ended
in this shape (audits awaiting review, plans awaiting a go, pipelines
paused at a gate, "want me to…?" closes).

## The bug before this fixture

Section B had no rule for "deliverable produced, awaiting your review."
So this state fell through to whichever later rule matched the
mechanical signals:

- **idle > 1800s + clean cwd + a recap that says "done"** → B1
  `ready_for_cleanup` → Assistant relays a `/cleanup` confirm card (or,
  on Assistant-merged workspaces, fires `/cleanup`), proposing to tear
  down the very artifact the operator still needs to read.
- **idle > 1800s + idle agent** → B3 `stranded` → Assistant nudges
  "please continue / resume" on top of work that is finished and
  waiting on the *human*, not the agent.

Both are wrong. The operator's exact complaint: "Observer keeps asking
those workspaces to resume or wrap up."

## What Observer must do now

New rule **B1 (work delivered, awaiting review)** fires first and emits
`needs_user`. That verdict only surfaces an awaiting card (see
`bin/pulse.py` execute_verdict) — it sends NOTHING into the workspace,
so no resume nudge and no /cleanup. The card's title/detail let the
operator act (review the audit, approve the rule wire-in) without
opening the workspace.

## Inputs

- `transcript.jsonl` — audit complete; last assistant turn hands back
  the audit path + two draft-rule paths and closes with "say the word
  and I'll wire in the rule + lint."
- `ctx.json` — `last_turn_age_sec: 9000` (2.5h idle), `agent_status:
  idle`, `cwd_dirty: false`, `cwd_unpushed: false`. This is precisely
  the signal combination that previously satisfied B1-cleanup AND
  B3-stranded. No PR (no fake-gh-bin), so Section A does not apply.

`forbidden_verdicts: ["ready_for_cleanup", "stranded"]` — either one is
the production misfire this fixture pins against. `ready_for_cleanup`
would propose discarding the deliverable; `stranded` would nudge a
finished workspace to "continue."

Expected: `needs_user`.
