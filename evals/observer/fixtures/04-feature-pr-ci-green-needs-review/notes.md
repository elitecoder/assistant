# 04 — feature PR + CI green → needs_user

PR title is `[FEATURE]`, files include production-code outside test paths,
body does NOT mention refactor or "no behavior change". CI is fully green.

Rule A3: PR is otherwise ready, CI green, but NOT auto-mergeable per
A1/A2. Observer must emit `needs_user` with title + detail summarizing
why review is needed.

If Observer emits `ready_for_merge` here, that's the worst-case bug —
auto-merging an unreviewed feature.
