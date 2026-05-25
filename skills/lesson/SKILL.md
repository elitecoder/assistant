---
name: lesson
description: Capture a rule into ~/.claude/CLAUDE.md so every future Claude Code session sees it. Use this whenever the user corrects you ("no", "don't", "stop", "wrong", "that's not", "I told you", "you keep", "again", "actually"), names a rule out loud ("always X", "never Y", "from now on Z", "remember this", "this needs to be a lesson", "make this an enforced lesson"), or you discover a non-obvious constraint that future sessions should respect. Also use when the user explicitly types /lesson. The skill writes a block to the `## Lessons` section of ~/.claude/CLAUDE.md via the assistant-curator CLI; CLAUDE.md auto-loads into every session so the rule propagates immediately.
---

# /lesson — capture a rule into ~/.claude/CLAUDE.md

The mechanism by which Claude Code agents learn from corrections. Lessons live inside the `## Lessons` section of `~/.claude/CLAUDE.md`. CLAUDE.md is officially auto-loaded by Claude Code into every session, so a rule captured here is enforced everywhere from the next conversation forward.

## When this skill MUST fire (auto-trigger, not just on `/lesson`)

If the conversation contains any of these signals, capture a lesson **before continuing the prior task**:

- **Correction signals**: `no`, `don't`, `stop`, `wrong`, `that's not`, `I told you`, `we discussed`, `you keep doing`, `again`, `damn`, `actually`.
- **Name-the-rule signals**: `always X`, `never Y`, `from now on Z`, `remember this`, `this needs to be a lesson`, `make this an enforced lesson`, `record this rule`.
- **Self-discovered constraint**: you tried something, hit an error, learned the actual rule. If a future session would benefit from that knowledge, capture it.

Do NOT silently absorb the correction and move on. The user has named a rule; future sessions need to see it. Capture the rule first, then continue the original task.

## What a lesson looks like

A lesson is a markdown block under `## Lessons` in `~/.claude/CLAUDE.md`:

```markdown
<!-- lesson: <slug>, scope: <scope>, added: <YYYY-MM-DD> -->
**<one-line trigger sentence>** <rule body, one paragraph, specific and actionable>
```

There is **no "why" field**. A rule is a rule — the body should be self-contained.

- **Trigger** — one sentence describing the *condition* the rule applies to. Not the rule itself, the *situation*. Good: `Spawning a workspace via the spawn-claude-workspace skill`. Bad: `When the user said "use sonnet" once on May 22`.
- **Rule** — one paragraph max. Specific. Actionable. Good: `Pass MODEL=sonnet for routine work, MODEL=opus for decision-making.` Bad: `Be careful about model selection.`

## How to invoke

```bash
~/.claude/bin/assistant-curator.py write \
  --trigger "<situation this rule fires in>" \
  --rule    "<what to do or not do — be specific and actionable>" \
  --scope   "global|classification|dashboard|ffp|scout|memory|security"
```

The curator picks a slug from the trigger, prepends the scope if non-global, and appends the block under `## Lessons`. It refuses to overwrite an existing slug.

## Procedure

1. **Identify the trigger and rule.** Re-read the user's last 1–2 messages. The user's words are usually the rule itself; your job is to extract the *condition* (trigger) and the *constraint* (rule) and phrase them in the third person so future sessions can apply them.

2. **Decide where the rule belongs.** Lessons in CLAUDE.md auto-load into EVERY Claude Code session, so they should only be rules that genuinely apply to every session. If the rule is **dispatcher-specific** (about how the Assistant agent in `~/dev/assistant/` decides when to spawn workspaces, what model to use, when to flip TODO status, etc.), it does NOT go in CLAUDE.md — it goes in the Assistant's own prompt at `~/dev/assistant/prompts/prompt-assistant-agent.md` under `## Assistant policies`. The curator refuses scopes `dispatch` and `todo` for this reason. If the rule applies to any user, any session, in any context — proceed; it belongs in CLAUDE.md. Otherwise tell the user "this looks dispatcher-specific; should it go in the Assistant prompt instead?"

3. **Choose a scope.** One of:
   - `global` — applies to any session
   - `classification` — categorizing TODOs, PRs, sessions
   - `dashboard` — UI rendering, widget behavior
   - `ffp` — anything specific to firefly-platform / Squirrel that ALL FFP-touching sessions need (not just the dispatcher)
   - `scout` — Scout MCP usage
   - `memory` — auto-memory and lesson handling itself
   - `security` — destructive ops, credential handling, permission widening
   - **NOT allowed** (dispatcher-only, go in the Assistant prompt instead): `dispatch`, `todo`

3. **Run the curator.** Quote any user-provided text verbatim where possible. If the user said "always X", the rule should literally start with "Always X".

4. **Confirm to the user.** Output the captured block (slug + trigger + rule) so they can correct the wording before the conversation moves on.

5. **Resume the original task.** The lesson is now in CLAUDE.md and applies to every future session — including the next turn of the current session if you re-read CLAUDE.md.

## Other curator subcommands

```bash
~/.claude/bin/assistant-curator.py list                 # show all lessons
~/.claude/bin/assistant-curator.py list --scope ffp     # filter by scope
~/.claude/bin/assistant-curator.py rm <slug>            # remove a stale lesson
~/.claude/bin/assistant-curator.py trim                 # open CLAUDE.md in $EDITOR for triage
```

## Anti-patterns

- **Silently absorbing the correction.** "Sorry, I'll do better next time" without writing a lesson means the next session repeats the mistake. Always capture.
- **Capturing the conversation, not the rule.** A lesson is timeless. "Don't tell ws:9001 to skip G3" is not a lesson; "Never tell any session to skip the local G3 E2E suite — Jenkins runs a subset" is the lesson.
- **Adding a why.** The format has no `--why` flag. The rule body should explain itself; if it doesn't, rewrite the rule, don't add justification.
- **Pinning everything.** There is no `--pin` flag. Rules trim manually via the user invoking `/lesson trim` (which opens CLAUDE.md in `$EDITOR`).

## Examples

User says: *"Jenkins does not run the full suite. This needs to be an enforced lesson."*

```bash
~/.claude/bin/assistant-curator.py write \
  --scope ffp \
  --trigger "An in-flight FFP archffp pipeline is at G3 and someone is tempted to skip the local E2E suite because 'Jenkins will catch it'" \
  --rule "Never tell a session to skip, abort, or bypass the local G3 E2E suite. Jenkins runs a SUBSET of the firefly-platform Squirrel E2E suite, not the full suite. Test-only PR / unit-writer-green / Jenkins-will-catch-it are not valid reasons. If the local suite is over-saturated, wait; never skip."
```

User says: *"Always pass --focus false when spawning a workspace from a background script."*

```bash
~/.claude/bin/assistant-curator.py write \
  --scope dispatch \
  --trigger "Spawning a workspace from a background script (cron, watchdog, hook)" \
  --rule "Always pass --focus false. Background spawns must never steal the user's foreground tab. The cmux flag is mandatory; there is no per-call exception."
```
