# 09 — adversarial: prose mentions merged PR but workspace owns NO PR

This is the production bug from 2026-05-26: the agent's transcript
mentions PR #10319 (an unrelated W1 chore that was already merged) in
its narrative — only as a reference point, not as the workspace's own
work. The workspace is mid-audit, no PR opened for this branch.

The adversarial fake-gh-bin trap: if Observer foolishly does
`gh pr view 10319` (using the prose-mentioned number), it sees MERGED
and may incorrectly emit `ready_for_cleanup` or `ready_for_merge`.

The CORRECT behavior is to query by the workspace's branch:
`gh pr view --head $(git -C <cwd> branch --show-current)`. The fake gh
returns "no pull requests found" for that — meaning no PR for this
workspace. Falls through to ruleset B. Last assistant text is
mid-narrative ("Moving to spec 9"), so:

  - last_turn_age_sec=1500 (25 min) — borderline for B3 stranded
    threshold (1800s = 30min). Observer should pick `active`.
  - The transcript shows continuous progress, no idle pause.

Expected: `active`. (`stranded` is also acceptable if Observer reads
the time threshold loosely — we'd need to tighten the test if we want
strict.) The KEY assertion is **NOT** `ready_for_merge` or
`ready_for_cleanup` — those are the broken behaviors we're guarding
against.

## What this test catches

If the Observer prompt ever drifts back toward "extract PR numbers
from prose and trust them," this test fires. The adversarial gh shim
deliberately rewards the wrong behavior (MERGED on a positional
lookup) and punishes the right behavior (no PR for branch).
