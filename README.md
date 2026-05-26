# Assistant

Mukul's personal dispatcher for parallel cmux Claude Code workspaces.

The Assistant runs as a Sonnet 1M Claude session in a cmux workspace titled `Assistant (Sonnet 1M)`. A LaunchAgent pulses it every 2 minutes. On each pulse the Assistant orchestrates everything cross-workspace; a per-workspace **Observer** subagent reads each workspace's transcript and emits one of five verdicts; the Assistant relays the matching slash command into the workspace.

## Control loop

```
┌─────────────────────────────────────────────────────────────────┐
│  Cron (every 2 min) → assistant-pulse.sh                        │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ASSISTANT (Sonnet, mostly mechanical orchestration)            │
│   1. Drain ~/.assistant/inbox/                                  │
│   2. Run bin/purge-stale-awaiting.py                            │
│   3. Dispatch new TODOs (priority + active-cap, mechanical)     │
│   4. For each cmux workspace:                                   │
│        ctx = build-ws-context.py --ws-ref <ws>                  │
│        verdict = Observer(ctx)         ← LLM, isolated          │
│        execute(verdict)                ← table lookup           │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  OBSERVER (one LLM call per workspace per pulse)                │
│  Reads the workspace's session JSONL directly via bash.         │
│  Fetches PR state on demand: gh pr view --head <branch>.        │
│  Emits ONE verdict from a closed vocabulary:                    │
│                                                                 │
│   {"verdict": "ready_for_merge"}                                │
│   {"verdict": "ready_for_cleanup"}                              │
│   {"verdict": "stranded",   "nudge_text": "..."}                │
│   {"verdict": "needs_user", "title": "...", "detail": "..."}    │
│   {"verdict": "active"}                                         │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Assistant executes:                                            │
│   ready_for_merge   → cmux-send.py --text "/merge-when-ready"   │
│   ready_for_cleanup → cmux-send.py --text "/cleanup"            │
│   stranded          → cmux-send.py --text "<nudge_text>"        │
│   needs_user        → append to awaiting_input[]                │
│   active            → no-op                                     │
└─────────────────────────────────────────────────────────────────┘
```

Slash commands run **inside** the workspace and own their own state — branch, PR number, TODO id. The Assistant never threads parameters through; it just relays the bare slash command.

The Assistant **never** closes cmux workspaces. That's the user's job. Removed 2026-05-26 after auto-close hid in-progress research mid-flight.

## Components

| File | Cadence | Role |
|---|---|---|
| `bin/assistant-pulse.sh` | LaunchAgent every 2 min | Wakes the Assistant via inbox + Enter keypress |
| `prompts/prompt-assistant-agent.md` | re-read each pulse | Assistant orchestration prompt |
| `prompts/observer-prompt.md` | re-read per Observer call | Observer per-workspace verdict prompt |
| `bin/build-ws-context.py` | called per workspace per pulse | Builds the JSON ctx Observer reads |
| `bin/purge-stale-awaiting.py` | called by pulse Step 2 | Drops awaiting cards whose underlying state changed |
| `bin/cmux-send.py` | called by every send action | Single sanctioned send path; logs literal text + transcript-byte delta |
| `bin/state-write.py` | end of pulse | Atomically writes assistant-state.json + per-pulse trace |
| `bin/render-assistant-page.py` | LaunchAgent every 15s | Renders dashboard HTML |
| `bin/todo-server.py` | HTTP daemon on 127.0.0.1:9876 | Powers dashboard buttons |
| `bin/session-context-watcher.py` | event-driven (kqueue) | Tails Claude JSONL transcripts |

## Repo layout

```
assistant/
├── bin/                      # daemons, helpers, the per-pulse pipeline
├── prompts/
│   ├── prompt-assistant-agent.md   # Assistant orchestration
│   └── observer-prompt.md          # Observer per-workspace verdicts
├── skills/                   # /todo, /cleanup, /spawn-claude-workspace, /cmux
├── evals/
│   ├── observer/             # 9 fixtures × Sonnet → verdict assertions
│   ├── assistant-cleanup-gating/
│   ├── assistant-dispatch-validation/
│   ├── assistant-inflight-check/
│   └── assistant-stale-card-purge/
├── tests/                    # Pure-Python unit tests (no LLM)
├── launchagents/             # macOS LaunchAgent plists
└── docs/                     # operating guide
```

## Where the live system reads from

This repo is the source of truth. The running system reads from `~/.claude/` and `~/Library/LaunchAgents/`. Wire-up via `install.sh`:

```bash
./install.sh              # dry-run, shows what would change
./install.sh --apply      # actually wires it up
```

| Repo path | Live path | Mechanism |
|---|---|---|
| `bin/` | `~/.claude/bin` | symlink |
| `prompts/*.md` | `~/.claude/spawn-prompts/*.md` | symlink |
| `docs/assistant-operating-guide.md` | `~/.claude/assistant-operating-guide.md` | symlink |
| `skills/<name>/` | `~/.claude/skills/<name>/` | **copy** (so each skill is a self-contained, shareable artifact) |
| `launchagents/*.plist` | `~/Library/LaunchAgents/*.plist` | copy + launchctl reload |

Skills edited in place at `~/.claude/skills/<name>/` can be pulled back with `./install.sh --pull-skills`.

## Testing — both LLM and mechanical

### Observer eval (LLM)

```bash
cd evals/observer
./run.py                             # all 9 fixtures (~140s)
./run.py 01-ws97-trap-no-pr-mid-audit
EVAL_MODEL=us.anthropic.claude-sonnet-4-6[1m] ./run.py
```

Each fixture has a real or synthesized session JSONL, an `expected.json`, and an optional `fake-gh-bin/gh` shim mocking `gh pr view`. Fixtures may declare `forbidden_verdicts` — if Observer emits one of those, the runner reports `DANGEROUS:` rather than just a verdict mismatch.

The headline regression-pin (fixture 01) replays the actual production-bug transcript: agent 6 of 14 specs into a phonebook-retirement audit, prose mentions an unrelated merged PR. The old pipeline auto-closed the workspace; the new pipeline must emit `active`.

### Assistant unit tests (Python, no LLM)

```bash
python3 -m unittest discover tests -v        # ~2s
```

| File | Pins |
|---|---|
| `test_purge_stale_awaiting.py` | drop predicates: closed workspaces, done TODOs, cmux-down safety. |
| `test_build_ws_context.py` | mechanical signals; no PR data leaks into output. |
| `test_no_close_workspace.py` | regression-pin: no production code path shells out `cmux close-workspace`. |

Run after any change to `prompts/observer-prompt.md`, `bin/build-ws-context.py`, or any of the verdict-execution paths.

## Daily workflow

```bash
# Edit code in the repo — it's live (symlink).
$EDITOR bin/build-ws-context.py
# Next pulse picks it up automatically; no daemon reload needed.

# Edit a prompt — re-read each pulse.
$EDITOR prompts/observer-prompt.md
cd evals/observer && ./run.py    # verify behavior didn't regress

# Edit a skill in the repo — re-run install.
$EDITOR skills/cleanup/cleanup.sh
./install.sh --apply

# Edit a plist — re-run install (launchctl-reloads it).
$EDITOR launchagents/com.assistant.assistant-pulse.plist
./install.sh --apply

# Stop the assistant pulse:
launchctl unload -w ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist
launchctl load   -w ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist
```

## Runtime state — NOT committed

Regenerated continuously under `~/.claude/cache/` and `~/.assistant/`:

- `~/.claude/cache/assistant-state.json` — Assistant decisions (most recent pulse)
- `~/.claude/assistant-todo.json` — TODO data
- `~/.claude/cache/world.json` — workspace inventory (still emitted by `world-scanner`)
- `~/.assistant/heartbeat.json` — Assistant workspace ref
- `~/.assistant/sends.jsonl` — every cmux send (literal text + byte delta)
- `~/.assistant/actions-ledger.jsonl` — every action with `verified_via` proof field
- `~/.assistant/pulse-trace/pulse-<idx>-<ts>.md` — per-pulse trace (Observers, decisions, sends, ledger writes, cross-checks)
- `~/.architect/orchestrator-ledger/cleanup-*.json` — `/cleanup --undo` history

## Lessons

Rules learned from past incidents live inside `~/.claude/CLAUDE.md` under a `## Lessons` heading. CLAUDE.md is auto-loaded by Claude Code into every session, so the rules apply to ad-hoc Claude calls without extra wiring. The repo doesn't track lessons.

```bash
~/.claude/bin/assistant-curator.py write \
  --trigger "<one-line situation>" \
  --rule "<what to do or not do>" \
  --scope "global|dispatch|dashboard|todo|ffp|security|..."
```

Subcommands: `list`, `rm <slug>`, `trim` (opens CLAUDE.md in `$EDITOR`).

## Design notes

- **Two LLMs share NO rule table.** The Observer applies its own ruleset (in `prompts/observer-prompt.md`) and emits one verdict. The Assistant looks up that verdict in a small static table and executes the matching action. Neither LLM applies the *other's* rules.
- **PR data is fetched by branch, not prose.** `gh pr view --head <branch>` from cwd. No regex-scraping PR numbers out of the agent's narrative — that pipeline (deleted 2026-05-26) caused the production bug where unrelated merged PRs drove workspace-close decisions.
- **The Observer reads the JSONL directly.** No curated turns, no truncation. If it needs the original prompt, it `head -30`s; if it needs to know what tools ran, it scrolls back. The transcript is ground truth.
- **`/cleanup` and Assistant both refuse to close workspaces.** That's the user's call. The skill records a `skipped: workspace closure disabled by policy` step and exits. Auto-close is gone.
- **Single sanctioned send path.** Every keystroke the Assistant issues goes through `bin/cmux-send.py` so the literal text + post-send transcript-byte delta are logged. Without that, "I sent it" is unverifiable.
