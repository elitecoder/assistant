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

## Dispatcher skipped the merge-pr safety gate (2026-05-25, pulse 72) {#dispatcher-skipped-safety-gate}

After the safety gate landed in commit 868c0af (test-only OR refactor-attested), the dispatcher at pulse 72 (18:35Z) STILL fired `/merge-when-ready 10349` against PR #10349 — a `[FEATURE]` PR touching `cloud-sync-bar-presentation.ts` (production code). The ledger evidence said "PR #10349 CI green, /merge-when-ready 10349 sent to ws:20 screen" — no mention of the file-list check Step 0 requires.

Root cause: the safety gate was prose instruction in the prompt's `merge-pr` action implementation. Under load, Sonnet shortcuts multi-step prose: it remembers "merge-pr → send slash command" and skips "first read files,title,body and verify rule (a) or (b)." The dispatcher logged `outcome: verified` based on the cmux RPC return code, not on whether the gate had been evaluated.

Compounded by [INCIDENTS.md#send-text-not-submit](#send-text-not-submit): even the slash command that did get sent used `cmux send-text` which never submitted — so the production-code PR was both incorrectly accepted by the safety gate AND silently failed to dispatch.

**Rule:** the safety gate, CI routing, and post-send verification all live inside `bin/merge-pr-dispatch.py`. The dispatcher MUST invoke that script for every `merge-pr` action; the script's exit code dictates the ledger outcome. Decisions (whether to propose merge-pr at all) stay in the observer prompt; mechanical checks (file-list parsing, transcript matching) stay in the script and are unbypassable. The observer's own enforcement is belt; the script is suspenders.

## merge-pr safety gate {#merge-pr-safety}

The two-branch router can land a PR without human review. That's fine for test-only PRs and for refactors with full local G3 + unit suite green — both have low blast radius. For feature/bugfix PRs (any production-code touch), human review is mandatory. Without a Step-0 safety gate, an aggressive observer proposing `merge-pr` on any OPEN PR would land production code unreviewed.

**Rule:** Step 0 of the `merge-pr` router refuses any PR that doesn't qualify for one of: (a) test-only paths, OR (b) refactor + full local G3 + unit suite green. Otherwise log `merge-pr-refused:not-auto-mergeable` and emit awaiting card "PR needs human reviewer."

## /merge-when-ready 10362 sent to wrong workspace (2026-05-26) {#screen-read-misroute}

Pulse 4 of session `f58ccdbb` produced two false-verified ledger entries on the same pulse:

1. `assistant:merge-pr:10362:ws1` — `/merge-when-ready 10362` was supposedly sent to **ws:1 (E2E Reliability)** to merge PR #10362 (the deflake-config-smoke-61 PR, owned by ws:57). Evidence: "confirmed on ws:1 screen ('This is added function/merge-when-ready 10362')". Reality: ws:1's transcript JSONL had ZERO occurrences of `ARGUMENTS: 10362`; the keystrokes never reached the claude PID — they trampled Mukul's in-progress design-doc message in the input box. The "evidence" was a screen-read hallucination concatenating Mukul's actual text "This is added functionality" with another fragment.

2. `assistant:cleanup:workspace:90` — `cleanup` sent to ws:90 (`Auto: td-037`) on the claim that td-037 was complete with PR #54 opened. Reality: td-037 had been dispatched only 17 min earlier; no archffp pipeline can finish that fast, and PR #54 was never validated via `gh pr view`.

**Two distinct bugs:**

- **(a) Workspace target override.** The Observer's verdict for PR #10362 was `params.ws_ref: workspace:57`. The Assistant overrode that to `workspace:1` on its own reasoning ("APPROVED + CLEAN + all CI green — strongest evidence"). There was no rule forbidding the override.
- **(b) Screen-read evidence accepted as proof of submission.** The cmux RPC return code said "OK" but the keystrokes never reached the claude PID (cmux silently drops sends to unfocused or non-terminal-active panes for some workspace topologies). The Assistant verified by reading `cmux rpc surface.read_text` and finding text patterns it expected. The terminal screen routinely loses scrollback, mangles ANSI, AND can echo a different workspace's surface back when surface refs collide across windows.

**Rules:**

1. **Workspace-target lock.** For every action that targets a workspace (`cleanup`, `nudge`, `merge-pr`, `close-workspace`), the `ws_ref` is inherited verbatim from the per-ws Observer's `params.ws_ref` OR the TODO's `dispatchedWs`. The Assistant prompt MUST NOT override it. If the Assistant believes the Observer's target is wrong, it surfaces a card — it does not "pick a better workspace."
2. **Screen reads are NOT evidence.** Ledger field `verified_via=screen_read` is rejected as a class. The acceptable evidence types are `jsonl_transcript`, `transcript_size_delta` (from `cmux-send.py`), `exit_code` (from a wrapper script that already verified), `gh_pr_view`, and `observer_summary` (only for informational/purge actions). Anything else logs `verified_via=not_verified` and `outcome=failed`.
3. **All sends go through `bin/cmux-send.py`.** The script captures `transcript_size_delta` (post-send byte growth). A delta of 0 means cmux returned OK but the keystrokes did not reach claude — fail fast. Logged to `~/.assistant/sends.jsonl`.
