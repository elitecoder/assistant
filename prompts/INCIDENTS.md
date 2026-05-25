# Assistant — incident log

Historical incidents that motivated rules in `prompt-assistant-agent.md`.
Cite by anchor when the rule needs a "why."

## td-019 (2026-05-22) — duplicate spawn for in-flight work {#td-019}

ws:98 was hand-spawned at 01:45Z to ship td-019 and was actively shipping the PR. At 05:51Z Triage's Bucket B logic looked at td-019 (`autoDispatch=true`, `dispatchedAt` empty) and spawned ws:114 to do the same thing. Both workspaces force-pushed to the same branch on PR #10164.

**Rule:** pre-dispatch in-flight check — scan `world.live_sessions[]` and `world.workspaces[]` for any workspace whose title contains the td-id literally OR 2+ distinctive words from the TODO title OR cwd matches the TODO's worktree path. If matched, do NOT spawn; bind the TODO to that ws and surface a low-tier confirm-binding card.

## ws:130 .focused vs .caller (2026-05-22) {#focused-vs-caller}

An earlier `cmux identify --json` parser used `.focused.*` instead of `.caller.*`. `.focused` is whatever tab Mukul is currently looking at — which could be anything. The Assistant wrote workspace:130 (Mukul's focused tab) into its heartbeat; the pulse script then woke that wrong workspace.

**Rule:** always use `.caller.workspace_ref` (the workspace whose shell ran the command). Canonical doc: `~/.claude/skills/cmux-workspace/SKILL.md`.

## ws:28 / ws:29 stranded dispatches (2026-05-23) {#stranded-dispatch}

td-034 (ws:28) and td-035 (ws:29) had their prompts pasted into the input box during shell init but never submitted. Both workspaces sat at `context -- │ 0↑/0↓ │ $0.00` for hours doing nothing while their dispatch entries logged success.

**Rule:** mandatory post-spawn validation — search the spawned cwd's project directory for a fresh jsonl with a `type: user` line containing the prompt-file signature. Implementation: `bin/verify-spawn-submitted.py`. If validation fails, ONE recovery attempt (Return keypress, then re-paste); if still failing, log `dispatch-failed`, do NOT log `dispatch:`, do NOT set `dispatchedAt`. Surface awaiting card.

## pulse 122 stale awaiting cards (2026-05-23) {#stale-awaiting}

Pulse 122 inherited an `assistant:autodispatch-unset:bulk` card listing td-003/005/006/007/008/010/011 as `autoDispatch=null`. Between pulses, Mukul ran a bulk-flip script setting all of them to `true`. Pulse 122 re-emitted the card without re-checking and failed to dispatch any of the now-eligible TODOs. 7 dispatchable items sat idle ~30 min until Mukul nudged manually.

**Rule:** every awaiting card from the prior pulse is a hypothesis. Re-validate its predicate against current state before re-emitting. If the predicate is FALSE, drop the card and add an `assistant:awaiting-purged:<original-key>` action.

## td-026 NAB auto-flip (2026-05-23) {#nab-auto-flip}

td-026 was reported in Slack and auto-flipped to `deferred` after the spawned archffp agent filed it as NAB and transitioned the Jira to Done. Neither Mukul nor the original reporter reviewed the NAB conclusion. If the agent was wrong, the bug now sits silently closed.

**Rule:** NAB verdicts NEVER auto-flip TODO status. NAB is a judgment about whether the original report is valid, not an observable execution outcome. Surface an `assistant:nab-review:td-NNN` card requiring Mukul + reporter to compare notes before any defer.

## ws:42 + ws:43 double-spawn (2026-05-23) {#spawn-lock}

Cron pulse and a manual respawn ran within ~2s. Both passed the "alive?" check, both created workspaces, both ran zombie cleanup before seeing each other. Two Assistants alive simultaneously.

**Rule:** `spawn-assistant.sh` uses an atomic mkdir lockdir at `~/.assistant/spawn-assistant.lock`; stale locks (>5 min) are cleaned up.

## ws:23 archffp halted-pipeline close (2026-05-25) {#archffp-halted-close}

ws:23 was closed after an archffp pipeline halted at code-review (binding-element.md + lit.md violations). The worktree `archffp-export-progress-modal-runner-factory` contained fully-written implementation, unit tests, and a passing G3 run. All work was abandoned when the workspace was closed without capturing a follow-up. Mukul had to manually identify the worktree and request a resume.

**Rule:** never close a workspace with in-progress work without first ensuring the work is safe. Check `git status` + `git log origin/HEAD..HEAD`; if a pipeline halted mid-flight, surface an awaiting card; if there's deferred follow-up, create a new TODO with full context BEFORE closing.

## pulse 348 context collapse (2026-05-24) {#context-collapse}

Long-running Sonnet 1M session at pulse_idx=348, 51% context, started emitting `Pulse N. No actions.` one-liners instead of running Step 1-5. `actions_taken=[]`, awaiting_input was hours-stale, 7 done-shipped workspaces missing cleanup-gated cards. The Assistant wasn't broken — its own attention had collapsed under accumulated pulse history.

**Rule:** self-respawn at `pulse_idx ≥ 150`. Write a heartbeat with `status: respawn-requested` AND back-date `last_pulse_ts` by 700s so the watchdog respawns next tick. End the turn before running Step 1.

## PR #10325 / #10305 stuck merge (2026-05-25) {#stuck-merge}

Dispatcher's `merge-pr` action ran `gh pr merge --auto --squash <PR>` directly, exit-code-0 logged as `verified`, then a dedupe rule on key `assistant:merge-pr:<PR>` shielded the PR from any retry. Both PRs sat OPEN+REVIEW_REQUIRED+auto-merge-queued for hours because the queue command queues but never approves; only `/merge-when-ready` posts the `!approve` bot trigger.

**Rule:** `merge-pr` is a dumb router driven by observable PR state — never `gh pr merge` directly. CI green → `/merge-when-ready`; CI not green → `/monitor-ffp-ci`. Skills are idempotent; `merge-pr` is exempt from the dedupe rule. Stops only on `state: MERGED` or `state: CLOSED`.

## send-text vs send — staged-but-unsent slash command (2026-05-25) {#send-text-not-submit}

After the Assistant respawned under the new merge-pr router, pulse 72 dispatched `/merge-when-ready 10342` to ws:6 and `/merge-when-ready 10349` to ws:20 via `cmux send-text`. The slash command was typed into both input prompts but never submitted — `cmux send-text` only types, it does NOT press Enter. The dispatcher logged `outcome: verified` from the cmux exit code (which only meant the keystrokes were accepted, not that the command was submitted). Both PRs sat with the command staged-but-unsent for 30+ minutes.

**Rule:** for any slash-command dispatch into a workspace, use `cmux send` (not `cmux send-text`) — `send` types AND presses Enter. After dispatch, re-read the target's transcript via `transcript-tail.py --ws <ws_ref>` and confirm `last_user.text` matches the literal slash command. cmux exit-code-0 is NOT proof of submission.

## Observer stuck on prior summary (2026-05-25) {#observer-stuck}

After the merge-pr router fix landed, observers for ws:4 (PR #10341), ws:6 (PR #10342), and ws:12 (PR #10305) classified those workspaces as AWAITING_USER / DONE with `proposed_actions: []` for hours, even though the PRs were OPEN+REVIEW_REQUIRED with auto-merge queued — exactly the state the merge-pr rule is supposed to fire on. Routine Sonnet observers were pattern-matching their own prior `summary_for_next_pulse` ("Auto-merge queued. REVIEW_REQUIRED. Close once merged.") instead of re-reading current world state. Other workspaces (ws:11 PR #10320) had correct observers that proposed merge-pr and dispatched cleanly. The bug was non-determinism in the Sonnet observer's verdict: same input, different output across pulses.

**Rule:** track `state_hash` (classification + summary + sorted action kinds) per observer summary. When a workspace's hash has been unchanged for >2h (`state_unchanged_since_ts`), spawn a fresh **Opus** sub-agent to re-classify with explicit "break the stalemate" framing. Cap 3 escalations per pulse.

## merge-pr safety gate {#merge-pr-safety}

The two-branch router can land a PR without human review. That's fine for test-only PRs and for refactors with full local G3 + unit suite green — both have low blast radius. For feature/bugfix PRs (any production-code touch), human review is mandatory. Without a Step-0 safety gate, an aggressive observer proposing `merge-pr` on any OPEN PR would land production code unreviewed.

**Rule:** Step 0 of the `merge-pr` router refuses any PR that doesn't qualify for one of: (a) test-only paths, OR (b) refactor + full local G3 + unit suite green. Otherwise log `merge-pr-refused:not-auto-mergeable` and emit awaiting card "PR needs human reviewer."
