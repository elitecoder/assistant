# 03 — refactor PR + CI green → ready_for_merge

Rule A2 happy path. The PR's title starts with `[REFACTOR]` and the body
explicitly says "no behavior change". CI all green. Files include
production-code (track-item-presentation.ts) — but the refactor exception
in A2 lets these merge automatically because behavior is preserved.

This is the test that catches a regression where Observer treats every
production-code PR as "needs_user" and never auto-merges refactors.
