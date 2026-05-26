# Observer eval — full run after prompt fix, 2026-05-26

## Result: 9/9 PASS

```
01-ws97-trap-no-pr-mid-audit          PASS  active             10.3s
02-test-only-pr-ci-green              PASS  ready_for_merge    17.2s
03-refactor-pr-ci-green               PASS  ready_for_merge    17.2s
04-feature-pr-ci-green-needs-review   PASS  needs_user         17.3s
05-done-recap-no-pr                   PASS  ready_for_cleanup  10.1s
06-question-to-user                   PASS  needs_user         15.4s
07-stranded-mid-task                  PASS  stranded           24.1s
08-active-working                     PASS  active             10.9s
09-adversarial-pr-prose-no-pr-actually PASS active             15.0s
```

Total: ~138s.

## What changed

The first run was 7/9 — fixtures 01 and 09 came back `stranded` instead
of `active`. The Observer was emitting `stranded` on workspaces idle
for just 10–25 minutes despite the prompt saying B3's threshold was
30 minutes (1800s).

Prompt fix made the threshold a hard numeric gate (with a cheat-sheet
table) and added explicit guidance for the borderline case: cron
pulses every 2 min mean an agent between tool calls can look idle for
5–25 min routinely; default to `active` until 30+ min.

## Regression-pin: forbidden_verdicts

Fixtures 01 (production-bug transcript) and 09 (adversarial — prose
mentions merged PR but workspace owns no PR) now declare
`forbidden_verdicts: [ready_for_cleanup, ready_for_merge]`. If the
Observer ever regresses to those answers on those fixtures, the runner
prints `DANGEROUS: ...` rather than just a verdict mismatch — making
the destructive-direction failure loud.
