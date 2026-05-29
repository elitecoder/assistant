# 11 — ws:55 eval-runner just emitted a per-case VERDICT, not a workspace recap

This is the actual transcript that triggered the production bug on
2026-05-28. Workspace:55 was the **eval runner** for archffp
code-reviewer scaling-costs. It runs the squirrel-code-review skill
across multiple cases (inline-magic, pragmatic-mitigation, etc.) on
unamended/amended worktree pairs. The agent had just emitted

    VERDICT: BLOCK

for **case 1 of N** — the per-case wrapper line, not a workspace-level
recap. `last_turn_age_sec` was 0 (the agent literally just spoke), and
cwd was clean (worktrees are git-clean by construction).

Observer's verdict at the time:

    {"ws_ref": "workspace:55", "verdict": "ready_for_cleanup",
     "summary": "Eval code-reviewer completed a BLOCK verdict with 4
                 Critical rule-slippage findings on branch
                 eval/scaling-costs/case-1-inline-magic-...-unamended
                 — no PR, cwd clean.",
     "next": "Assistant will send /cleanup to tear down the workspace."}

The Assistant pulse acted on it 13 minutes later and sent `/cleanup`
to a session that was about to start case 2. /cleanup tearing down a
mid-flight multi-case eval is exactly the destructive bug class this
suite was built to catch.

**Why Observer was wrong:** rule B.1 in `observer-batch-prompt.md`
said "Last assistant text is a definitive recap → ready_for_cleanup"
with no idle-time threshold and no requirement to confirm the recap is
about the **workspace**, not a per-case sub-result. The cheat-sheet
table did say `idle > 1800s` was required, but the rule body did not,
so the model followed the more permissive rule body. A per-case
"VERDICT: BLOCK" was misread as a workspace-level recap.

**Expected verdict: active** — the agent just spoke 0 seconds ago, a
multi-case eval is mid-flight, no `/cleanup` should fire.

If Observer emits `ready_for_cleanup` here, this test fails — that's
the production regression we're guarding against.
