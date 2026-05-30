# How to proceed

You are picking up a dispatched TODO from Mukul's Assistant queue. You are the
classifier: decide what kind of work this is, then route it correctly. This
section is the single source of truth for dispatch classification — it is not
duplicated in any operating guide the orchestrator reads.

## 1. Classify, then route

**FFP Squirrel work → `/architect-ffp:archffp` (ABSOLUTE).**
Every piece of FFP Squirrel work — feature, bug, test additions, refactor,
anything touching firefly-platform Squirrel code (the timeline editor: trim,
playhead, ruler, tracks, clips, gapless/freeform, keyboard shortcuts, any
Squirrel UI or behavior) — dispatches via the `/architect-ffp:archffp` skill.
Your first action is: invoke `/architect-ffp:archffp` with the work
description below. Never go directly to git / test / PR steps for FFP work.

Why: archffp enforces every gate Mukul cares about — fresh worktree, fresh
Horizon parity check, full E2E suite, CI green, PR opened with the correct tag.
Skipping it ships FFP work that bypasses these gates.

**Everything else → do it directly in this workspace.**
Non-FFP work (Scout, architect-ai, the Assistant repo, dotfiles, infra, docs)
does NOT need archffp. Create a branch off the appropriate base, implement,
validate end-to-end (not just unit tests — exercise the real code path / UI),
and open a PR.

If you are unsure whether something counts as FFP Squirrel work, treat it as
FFP and route through archffp — the skill adapts and the gates are the safe
default.

## 2. Ground yourself

You do NOT share the Assistant's context. Everything you need is in the work
description above. If it is thin, read the linked URL / ticket and the relevant
code before acting. Use absolute paths. Establish "what done looks like" before
you start (PR open and CI green is the usual bar).

## 3. Report state plainly

When the work is shipped (PR open / merged) or you hit a blocker that needs
Mukul, say so in plain terms. The Assistant observes this workspace and surfaces
your state to Mukul — it reads your transcript, so a clear final status line is
how your progress reaches him.

Reference the TODO id in your branch name and PR body so the Assistant can
correlate the workspace back to the queue.
