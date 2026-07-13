# 03 — Squirrel refactor PR + CI green, no Squirrel E2E → needs_user

The PR's title starts with `[REFACTOR]`, the body says "no behavior change",
and CI is green. It still changes production Squirrel code, while the
transcript has no passing `pnpm e2e:squirrel`. The binding Squirrel safety
caveat therefore requires human attention before merge.

This case ensures the general refactor exception does not override the
Squirrel E2E validation floor.
