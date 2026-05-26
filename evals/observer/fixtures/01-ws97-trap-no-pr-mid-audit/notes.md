# 01 — ws:97 trap: mid-audit, no PR, prose mentions PR #10359

This is the actual transcript that triggered the production bug. The agent
was 6 of ~14 specs into a phonebook-retirement audit. No PR was opened in
this workspace. The agent's recap mentions "PR #10359" only as an unrelated
reference point.

The old prose-regex-scraping pipeline picked up "PR #10359" → fetched its
state (MERGED) → fed to the next Observer → "PR done → workspace done" →
fired close-workspace.

The cwd in this fixture is intentionally `/tmp/no-such-cwd-eval-fixture` so
that any attempt to `gh pr view` from cwd fails (no branch). The Observer
must rely on the transcript alone and conclude:

- No PR exists for this workspace.
- Last assistant text is a state recap of mid-progress, not "done".
- `cwd_dirty=false` and `cwd_unpushed=false` are coincidental (no cwd at all).

**Expected verdict: active** — work is mid-flight. Do nothing.

If the Observer emits `ready_for_cleanup` or `ready_for_merge` here, this
test fails — that's the production regression we're guarding against.
