# Observer eval — first full run, 2026-05-26

## Result: 7/9 PASS, 2 FAIL

```
01-ws97-trap-no-pr-mid-audit         FAIL  stranded  (expected active)
02-test-only-pr-ci-green              PASS  ready_for_merge        16.6s
03-refactor-pr-ci-green               PASS  ready_for_merge        31.7s
04-feature-pr-ci-green-needs-review   PASS  needs_user             16.6s
05-done-recap-no-pr                   PASS  ready_for_cleanup      14.9s
06-question-to-user                   PASS  needs_user             14.3s
07-stranded-mid-task                  PASS  stranded               15.7s
08-active-working                     PASS  active                 10.0s
09-adversarial-pr-prose-no-pr-actually FAIL stranded  (expected active)
```

## What the failures tell us

The two failures are BOTH `stranded` instead of `active`. **Crucially:**

- **Fixture 01** (the production-bug transcript) — Observer did NOT
  emit `ready_for_cleanup` or `ready_for_merge`. The regression is
  pinned. Observer treated 10-min idle + mid-narrative as `stranded`
  (which is conservative); the prompt says B3 fires at 30+ min idle.
- **Fixture 09** (adversarial — prose mentions merged PR but workspace
  owns no PR) — Observer did NOT auto-act on the prose-derived PR. It
  correctly relied on `--head <branch>` returning no PR, then fell
  through to ruleset B and emitted `stranded`. The trap was NOT taken.

The system is not doing dangerous things. Both failures are
threshold-calibration issues:

1. Observer is treating "idle + last assistant text was mid-narrative
   (not a recap)" as `stranded` even when `last_turn_age_sec < 1800`.
2. The prompt's B3 rule says explicitly `> 1800` is required.

## Open question (calibration, not safety)

Should B3's threshold be tightened in the prompt, or should fixtures
01/09 expect `stranded`? Both are defensible. Today's behavior errs
on the side of nudging too eagerly — which is recoverable (user gets
a "Please continue" message in the workspace, no work loss). The
production bug was the OTHER direction (closing workspaces too
eagerly), which is genuinely destructive.

I'm leaving the test suite green-vs-red as-is so the calibration
question is visible in CI.
