---
name: lesson
description: Capture a rule so future sessions or the Assistant's Observer obey it. Use whenever the user corrects you ("no", "don't", "stop", "wrong", "that's not", "I told you", "you keep", "again", "actually"), names a rule out loud ("always X", "never Y", "from now on Z", "remember this", "this needs to be a lesson", "make this an enforced lesson"), or you discover a non-obvious constraint. Also use when the user explicitly types /lesson. The skill writes a block via the assistant-curator CLI to one of two stores: ~/.claude/CLAUDE.md (every session) or the Assistant's Observer prompt (orchestration / verdict rules like "never send /cleanup to a session awaiting my review").
---

# /lesson — capture a rule into the right store

The mechanism by which the system learns from corrections. A lesson is a `## Lessons` block written into ONE of two stores: `~/.claude/CLAUDE.md` (auto-loaded into every Claude Code session) or `~/dev/assistant/prompts/observer-batch-prompt.md` (read only by the Assistant's Observer each pulse). Pick the store by what the rule governs — see "Two targets" below.

## When this skill MUST fire (auto-trigger, not just on `/lesson`)

If the conversation contains any of these signals, capture a lesson **before continuing the prior task**:

- **Correction signals**: `no`, `don't`, `stop`, `wrong`, `that's not`, `I told you`, `we discussed`, `you keep doing`, `again`, `damn`, `actually`.
- **Name-the-rule signals**: `always X`, `never Y`, `from now on Z`, `remember this`, `this needs to be a lesson`, `make this an enforced lesson`, `record this rule`.
- **Self-discovered constraint**: you tried something, hit an error, learned the actual rule. If a future session would benefit from that knowledge, capture it.

Do NOT silently absorb the correction and move on. The user has named a rule; future sessions need to see it. Capture the rule first, then continue the original task.

## Two targets — pick first

A lesson goes into ONE of two stores, chosen with `--target`:

- **`claude`** (default) → `## Lessons` in `~/.claude/CLAUDE.md`. Auto-loaded into **every** Claude Code session. Use ONLY for rules that genuinely apply to any session, any repo, any context.
- **`assistant`** → `## Lessons` in `~/dev/assistant/prompts/observer-batch-prompt.md`. Read **only by the Assistant's Observer** each pulse. Use for rules about how the Assistant orchestrates workspaces — verdict policy, when to merge/cleanup/nudge. These are binding overrides on the Observer's ruleset and must NOT pollute CLAUDE.md.

**Litmus test:** would a random coding session in an unrelated repo need this rule? Yes → `claude`. No, it's about how the Assistant judges/acts on workspaces → `assistant`.

Example of an `assistant` rule: *"Never send /cleanup to a session awaiting my review."* That's the Observer's verdict call — it belongs in the Observer prompt, not CLAUDE.md.

## What a lesson looks like

A lesson is a markdown block under `## Lessons` in the target store:

```markdown
<!-- lesson: <slug>, scope: <scope>, added: <YYYY-MM-DD> -->
**<one-line trigger sentence>**

<rule body, one paragraph, specific and actionable>
```

There is **no "why" field**. A rule is a rule — the body should be self-contained.

- **Trigger** — one sentence describing the *condition* the rule applies to. Not the rule itself, the *situation*. Good: `Spawning a workspace via the spawn-claude-workspace skill`. Bad: `When the user said "use sonnet" once on May 22`.
- **Rule** — one paragraph max. Specific. Actionable. Good: `Pass MODEL=sonnet for routine work, MODEL=opus for decision-making.` Bad: `Be careful about model selection.`

## How to invoke

```bash
# Every-session rule → CLAUDE.md (default target)
~/.claude/bin/assistant-curator.py write \
  --trigger "<situation this rule fires in>" \
  --rule    "<what to do or not do — be specific and actionable>" \
  --scope   "global|classification|dashboard|ffp|scout|memory|security"

# Assistant orchestration rule → Observer prompt
~/.claude/bin/assistant-curator.py write --target assistant \
  --trigger "<workspace situation the Observer judges>" \
  --rule    "<what verdict to emit / not emit>" \
  --scope   "verdict|merge|cleanup|stranded|general"
```

The curator picks a slug from the trigger, prepends the scope if non-default, and appends the block under `## Lessons` in the chosen target. It refuses to overwrite an existing slug, and refuses a scope that isn't valid for the target.

## Procedure

1. **Identify the trigger and rule.** Re-read the user's last 1–2 messages. The user's words are usually the rule itself; your job is to extract the *condition* (trigger) and the *constraint* (rule) and phrase them in the third person so future sessions can apply them.

2. **Decide where the rule belongs.** Use the litmus test: would a random coding session in an unrelated repo need this rule?
   - **Yes → `--target claude`** (CLAUDE.md, every session).
   - **No, it's about how the Assistant orchestrates workspaces:**
     - **Verdict policy** (when to merge / cleanup / nudge / wait — what the Observer decides) → `--target assistant`. This is the common case and what `/lesson` writes.
     - **Dispatch classification / routing** (FFP→archffp, what kind of work goes where) → not a lesson; edit `~/dev/assistant/prompts/dispatch-classification.md` directly.
     - **Mechanical orchestration** (caps, dispatch picking, send gating) → not a lesson; it's code, edit `~/dev/assistant/bin/pulse.py`.

   If you're unsure whether a rule is every-session or Assistant-only, ask the user which, naming the consequence: a `claude` rule loads into every coding session; an `assistant` rule only steers the Observer.

3. **Choose a scope.** For `--target claude`:
   - `global` — applies to any session
   - `classification` — categorizing TODOs, PRs, sessions
   - `dashboard` — UI rendering, widget behavior
   - `ffp` — firefly-platform / Squirrel rules ALL FFP-touching sessions need
   - `scout` — Scout MCP usage
   - `memory` — auto-memory and lesson handling itself
   - `security` — destructive ops, credential handling, permission widening

   For `--target assistant`:
   - `verdict` — general verdict-selection rule
   - `merge` — when to emit / withhold `ready_for_merge`
   - `cleanup` — when to emit / withhold `ready_for_cleanup`
   - `stranded` — when to emit `stranded` / what nudge to send
   - `general` — orchestration rule that doesn't fit the above

4. **Run the curator.** Quote any user-provided text verbatim where possible. If the user said "always X", the rule should literally start with "Always X".

5. **Confirm to the user.** Output the captured block (slug + trigger + rule + which target) so they can correct the wording before the conversation moves on.

6. **Resume the original task.** A `claude` lesson applies to every future session (and this one if you re-read CLAUDE.md); an `assistant` lesson takes effect on the Observer's next pulse.

## Other curator subcommands

```bash
~/.claude/bin/assistant-curator.py list                      # all lessons, both stores
~/.claude/bin/assistant-curator.py list --target assistant   # just the Observer's rules
~/.claude/bin/assistant-curator.py list --scope ffp          # filter by scope
~/.claude/bin/assistant-curator.py rm <slug>                 # remove (searches both stores)
~/.claude/bin/assistant-curator.py trim --target assistant   # open a store in $EDITOR for triage
```

## Anti-patterns

- **Silently absorbing the correction.** "Sorry, I'll do better next time" without writing a lesson means the next session repeats the mistake. Always capture.
- **Capturing the conversation, not the rule.** A lesson is timeless. "Don't tell ws:9001 to skip G3" is not a lesson; "Never tell any session to skip the local G3 E2E suite — Jenkins runs a subset" is the lesson.
- **Putting an Assistant rule in CLAUDE.md.** "Never send /cleanup to a session awaiting my review" is the Observer's verdict policy — it belongs in `--target assistant`, not CLAUDE.md. Loading it into every coding session is noise; loading it into the Observer is the point.
- **Adding a why.** The format has no `--why` flag. The rule body should explain itself; if it doesn't, rewrite the rule, don't add justification.
- **Pinning everything.** There is no `--pin` flag. Rules trim manually via the user invoking `/lesson trim` (which opens the store in `$EDITOR`).

## Examples

User says: *"Jenkins does not run the full suite. This needs to be an enforced lesson."*

```bash
~/.claude/bin/assistant-curator.py write \
  --scope ffp \
  --trigger "An in-flight FFP archffp pipeline is at G3 and someone is tempted to skip the local E2E suite because 'Jenkins will catch it'" \
  --rule "Never tell a session to skip, abort, or bypass the local G3 E2E suite. Jenkins runs a SUBSET of the firefly-platform Squirrel E2E suite, not the full suite. Test-only PR / unit-writer-green / Jenkins-will-catch-it are not valid reasons. If the local suite is over-saturated, wait; never skip."
```

User says: *"Never send /cleanup to a session that's awaiting my review."* — this is the Observer's verdict policy, so it goes to the assistant store, not CLAUDE.md:

```bash
~/.claude/bin/assistant-curator.py write --target assistant \
  --scope cleanup \
  --trigger "A workspace is awaiting Mukul's review (needs_user emitted, or the agent's last turn asks him a question / requests review)" \
  --rule "Never emit ready_for_cleanup for it. A session waiting on the operator must stay open until he acts; cleanup would discard work he hasn't seen. Prefer needs_user or active."
```
