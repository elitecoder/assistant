# 02 — test-only PR + CI green → ready_for_merge

The canonical happy path for rule A1. PR's `files` are exclusively under
`e2e/` (test specs + fixtures). `statusCheckRollup` is all-SUCCESS.
`mergeable: MERGEABLE`, no `CHANGES_REQUESTED`.

Observer must emit `ready_for_merge`. Assistant relays `/merge-when-ready`
to the workspace, and the slash command running inside the workspace
applies its own safety gate before queuing.

If Observer emits `needs_user` here, the system would never auto-merge
test PRs and the user gets babysitting work.

If Observer emits `ready_for_merge` for a NON-test PR (see fixture 04),
that's a different bug — but A1 requires the file-list to be all-test,
which the fake gh shim guarantees.
