# 06 — last assistant turn asks the user a question → needs_user

Rule B2. Agent emitted an explicit two-option question. Observer must
NOT nudge (it would insert "Please continue" on top of a pending
question). Must NOT cleanup (work isn't done, decision is pending).

Expected: `needs_user` — the title should reference the choice, the
detail should quote enough of the question that the user can answer
without opening the workspace.

This is a real-shape transcript from td-079 stacked-PR work.
