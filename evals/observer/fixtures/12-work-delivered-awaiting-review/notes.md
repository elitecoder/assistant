# 12 — work delivered, awaiting human review → needs_user (REAL ws:90 transcript)

This is the missed-state class the operator reported on 2026-05-30:
**work is complete, but a human must review it and give next steps.**
Not stalled — finished and correctly waiting. The verdict the operator
was correcting in the live conversation is pinned here.

## Provenance — this is a real transcript, not synthetic

- **Workspace:** ws:90, `coalesce-undo-audit td-098`.
- **Transcript:** sliced verbatim from the real session
  `~/.claude/projects/-Users-mukuls-dev-firefly-platform/03c1dd54-470c-4e46-9e13-00f46599fe1f.jsonl`
  (first user turn + the last 5 narrative turns — the audit → 12-rule
  HTML review dashboard → "hand me your verdicts and I'll execute"
  handoff arc).
- **ctx.json:** the real `build-ws-context.py` values Observer saw at
  run 0444 — `last_turn_age_sec: 3383`, `agent_status: idle`,
  `cwd_dirty: true`, `cwd_unpushed: true`.

The agent built a split-pane HTML dashboard embedding 12 draft Squirrel
enforcement rules, each with a before/after code pair and reviewer
flags, and closed with: *"Triage each: Install ✓ / Revise ✎ / Skip ✗ …
Hand me that exported JSON (or just tell me your verdicts) and I'll
execute exactly per your routing rules."* The deliverable is the
review dashboard; the next move is the operator's triage. Nothing is
activated — drafts only, per the operator's standing rule.

## The real misfire this pins against

Observer's verdict for ws:90 was **non-deterministic** across
consecutive pulses (`~/.assistant/observer-runs/`):

| run  | verdict        |
|------|----------------|
| 0440 | `needs_user` ✓ |
| 0442 | `needs_user` ✓ |
| 0444 | `active` ✗ (silent miss — no card surfaced) |
| 0446 | `needs_user` ✓ |
| 0447 | `needs_user` ✓ |

The `active` miss at 0444 is the failure: a finished deliverable
awaiting triage was classified as "mid-work, no action," so no awaiting
card was surfaced and the operator got no signal that 12 rules were
waiting on them. The sibling ws:89 in the same pulses got the worse
end of the same gap — an outright `stranded` "Please resume or wrap up"
nudge (see fixture 13).

## What Observer must do now

New Section-B rule B1 ("work delivered, awaiting human review or a
go-ahead") fires first → `needs_user`. That verdict only surfaces an
awaiting card (`bin/pulse.py` execute_verdict) — it sends NOTHING into
the workspace, so no resume nudge and no /cleanup. Idle time does not
gate B1, which is the property that kills the run-to-run flip-flop:
the same finished-deliverable recap returns `needs_user` regardless of
whether it is 3383s or 30000s old.

No PR exists for this workspace (no fake-gh-bin), so Section A does not
apply.

`forbidden_verdicts: ["ready_for_cleanup", "stranded"]` —
`ready_for_cleanup` would propose discarding the dashboard the operator
must still triage; `stranded` would nudge a finished workspace to
"continue." Either trips a DANGEROUS failure.

Expected: `needs_user`.
