# Assistant

Mukul's personal dispatcher for parallel cmux Claude Code workspaces.

`bin/pulse.py` runs as a `com.assistant.assistant-pulse` LaunchAgent every 5 minutes. Each pulse it picks up to 10 workspaces per Observer subprocess (parallel), spawns one `claude --print` session per batch with the `observer-batch-prompt.md` ruleset, reads each batch's `verdicts.jsonl`, and dispatches the matching slash command per workspace via a fixed lookup table. No LLM in the orchestration layer — the only LLM call is the per-batch Observer.

## Control loop

```
┌─────────────────────────────────────────────────────────────────┐
│  com.assistant.assistant-pulse LaunchAgent (every 5 min)        │
│   → /opt/homebrew/bin/python3 bin/pulse.py                      │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  pulse.py — mechanical orchestrator (no LLM)                    │
│   1. Drain ~/.assistant/inbox/                                  │
│   2. Run bin/purge-stale-awaiting.py                            │
│   3. bin/pick-ws-batch.py → to_reclassify[], backed_off[]       │
│      (back-off filter applied here, upstream of every step)     │
│   4. Build ctx for each ws via bin/build-ws-context.py          │
│   5. Chunk into batches of WS_BATCH_SIZE=10                     │
│   6. Spawn ceil(N/10) Observer subprocesses in parallel         │
│        ThreadPoolExecutor → claude --print → verdicts.jsonl     │
│   7. Read each batch's verdicts.jsonl from disk                 │
│   8. Per ws: save-ws-summary.py + execute via lookup table:     │
│        ready_for_merge   → cmux-send /merge-when-ready          │
│        ready_for_cleanup → cmux-send /cleanup                   │
│        stranded          → cmux-send <nudge_text>               │
│        needs_user        → append awaiting_input[]              │
│        active            → no-op                                │
│        no_action         → no-op (cleanup already ran)          │
│   9. NO_INGEST_GUARD — skip resends if last send was delta=0    │
│   10. state-write.py → assistant-state.json + per-pulse trace   │
│   11. heartbeat.json (drives dashboard health banner)           │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Observer subprocess (one per batch of ≤10 workspaces)          │
│   claude --print --dangerously-skip-permissions                 │
│   stdin = observer-batch-prompt.md + ctx JSON for batch         │
│   Reads each transcript via bash (tail -200 ...).               │
│   Writes JSONL to verdicts.jsonl (file, not stdout).            │
│   Stdout/stderr captured to observer-runs/ for audit.           │
└─────────────────────────────────────────────────────────────────┘
```

The orchestrator never closes cmux workspaces. That's the user's job. Slash commands run **inside** each workspace and own their own state — branch, PR number, TODO id.

## Components

| File | Cadence | Role |
|---|---|---|
| `bin/pulse.py` | LaunchAgent every 5 min | Mechanical orchestrator: pick → ctx → Observer batch → execute → trace |
| `prompts/observer-batch-prompt.md` | re-read per batch | Observer ruleset; outputs JSONL keyed by ws_ref |
| `bin/build-ws-context.py` | called per ws per pulse | Builds the JSON ctx Observer reads |
| `bin/purge-stale-awaiting.py` | called by pulse step 2 | Drops awaiting cards whose underlying state changed |
| `bin/pick-ws-batch.py` | called by pulse step 3 | Returns to_reclassify[] / backed_off[]; reads back-off.json |
| `bin/save-ws-summary.py` | per verdict | Persists per-ws verdict; rejects verdicts missing `next` |
| `bin/cmux-send.py` | per send action | Single sanctioned send path; logs literal text + transcript-byte delta |
| `bin/state-write.py` | end of pulse | Atomically writes assistant-state.json + per-pulse trace |
| `bin/back-off.py` | CLI (also `/back-off`, `/attend` skills) | Manages workspaces the orchestrator must skip |
| `bin/render-assistant-page.py` | LaunchAgent every 15s | Renders dashboard HTML; pulse-health banner reads heartbeat.json |
| `bin/todo-server.py` | HTTP daemon on 127.0.0.1:9876 | Powers dashboard buttons |
| `bin/world-scanner.py` | LaunchAgent every 30s | Builds the world.json snapshot the dashboard reads |
| `bin/session-context-watcher.py` | event-driven (kqueue) | Tails Claude JSONL transcripts |
| `bin/workspace-watcher.py` | LaunchAgent | Detects cmux workspace lifecycle events |
| `bin/cmux-ws-numberer.py` | event-driven (cmux events) | Appends `[N]` workspace-ref suffix to every cmux workspace title |
| `bin/assistant-curator.py` | CLI | Manages `## Lessons` block in `~/.claude/CLAUDE.md` |
| `bin/merge-pr-dispatch.py` | called by /merge-when-ready skill | Safety-gated PR merge dispatcher |

## Repo layout

```
assistant/
├── bin/                      # daemons, helpers, the per-pulse pipeline
├── prompts/
│   └── observer-batch-prompt.md  # the only LLM prompt in the loop
├── skills/                   # /todo, /cleanup, /spawn-claude-workspace, /back-off, /attend, /lesson
├── evals/observer/           # 9 fixtures × Sonnet → verdict assertions
├── tests/                    # Python unit tests (no LLM)
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
| `docs/assistant-operating-guide.md` | `~/.claude/assistant-operating-guide.md` | symlink |
| `skills/<name>/` | `~/.claude/skills/<name>/` | **copy** (so each skill is a self-contained, shareable artifact) |
| `launchagents/*.plist` | `~/Library/LaunchAgents/*.plist` | copy + launchctl reload |

Skills edited in place at `~/.claude/skills/<name>/` can be pulled back with `./install.sh --pull-skills`. The orchestrator reads `prompts/observer-batch-prompt.md` directly from this repo — no symlink.

## Observer audit trail

Every Observer call leaves a permanent on-disk record at `~/.assistant/observer-runs/<pulse_idx>/batch-<batch_idx>/`:

```
prompt.md         # full prompt sent to claude
ctxs.json         # input ctxs we asked it to judge
verdicts.jsonl    # claude wrote this — orchestrator reads ONLY this for verdicts
stdout.txt        # claude's --print stdout (work trail: tool calls, reasoning)
stderr.txt
meta.json         # rc, wall_ms, model, cmd, ws_refs, ts
```

Never deleted. Disk is cheap, audit trail is not.

## Testing

### Python unit tests (no LLM)

```bash
python3 -m unittest discover tests -v        # ~3s
```

| File | Pins |
|---|---|
| `test_pulse.py` | inbox drain, JSONL parse, verdict→action lookup, NO_INGEST_GUARD, chunker |
| `test_back_off.py` | back-off list filter is applied; CLI add/remove/list |
| `test_purge_stale_awaiting.py` | drop predicates: closed workspaces, done TODOs, cmux-down safety |
| `test_build_ws_context.py` | mechanical signals; no PR data leaks into output |
| `test_no_close_workspace.py` | regression-pin: no production code path shells out `cmux close-workspace` |

### Observer eval (LLM, real fixtures)

```bash
cd evals/observer
./run.py                             # all 9 fixtures (~140s)
./run.py 01-ws97-trap-no-pr-mid-audit
EVAL_MODEL=us.anthropic.claude-sonnet-4-6[1m] ./run.py
```

Each fixture has a real or synthesized session JSONL, an `expected.json`, and an optional `fake-gh-bin/gh` shim mocking `gh pr view`. Fixtures may declare `forbidden_verdicts` — if Observer emits one of those, the runner reports `DANGEROUS:` rather than just a verdict mismatch.

The headline regression-pin (fixture 01) replays the production-bug transcript: agent 6 of 14 specs into a phonebook-retirement audit, prose mentions an unrelated merged PR. The old pipeline auto-closed the workspace; the new pipeline must emit `active`.

Run after any change to `prompts/observer-batch-prompt.md`, `bin/build-ws-context.py`, or any of the verdict-execution paths.

## Daily workflow

```bash
# Edit code in the repo — it's live (symlink).
$EDITOR bin/build-ws-context.py
# Next pulse picks it up automatically; no daemon reload needed.

# Edit the Observer prompt — re-read each pulse.
$EDITOR prompts/observer-batch-prompt.md
cd evals/observer && ./run.py    # verify behavior didn't regress

# Edit a skill in the repo — re-run install.
$EDITOR skills/cleanup/cleanup.sh
./install.sh --apply

# Edit a plist — re-run install (launchctl-reloads it).
$EDITOR launchagents/com.assistant.assistant-pulse.plist
./install.sh --apply

# Trigger a pulse manually:
launchctl kickstart -k gui/$UID/com.assistant.assistant-pulse

# Pause / resume the orchestrator:
launchctl bootout  gui/$UID/com.assistant.assistant-pulse
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist
```

## Runtime state — NOT committed

Regenerated continuously under `~/.claude/cache/` and `~/.assistant/`:

- `~/.claude/cache/assistant-state.json` — orchestrator decisions (most recent pulse)
- `~/.claude/assistant-todo.json` — TODO data
- `~/.claude/cache/world.json` — workspace inventory (emitted by `world-scanner`)
- `~/.assistant/heartbeat.json` — pulse health, drives dashboard banner
- `~/.assistant/back-off.json` — workspaces the orchestrator must skip
- `~/.assistant/sends.jsonl` — every cmux send (literal text + byte delta)
- `~/.assistant/actions-ledger.jsonl` — every action with `verified_via` proof field
- `~/.assistant/observer-summaries/workspace_*.json` — most recent verdict per ws (powers dashboard Workspaces tab)
- `~/.assistant/observer-runs/<pulse_idx>/batch-<batch_idx>/` — full Observer call audit (kept forever)
- `~/.assistant/pulse-trace/pulse-<idx>-<ts>.md` — per-pulse trace
- `~/.architect/orchestrator-ledger/cleanup-*.json` — `/cleanup --undo` history

## Lessons

Rules learned from past incidents live inside `~/.claude/CLAUDE.md` under a `## Lessons` heading. CLAUDE.md is auto-loaded by Claude Code into every session, so the rules apply to ad-hoc Claude calls without extra wiring. The repo doesn't track lessons.

```bash
~/.claude/bin/assistant-curator.py write \
  --trigger "<one-line situation>" \
  --rule "<what to do or not do>" \
  --scope "global|classification|dashboard|ffp|scout|memory|security"
```

Subcommands: `list`, `rm <slug>`, `trim` (opens CLAUDE.md in `$EDITOR`).

## Design notes

- **Mechanical orchestrator, isolated LLM.** The Observer prompt is the only LLM in the loop and its only job is judgment. Verdict→action is a Python dict, not a prompt example the LLM can drift from. Bugs are diffs + unit tests, not prompt rewrites + restart cycles.
- **PR data is fetched by branch, not prose.** `gh pr view --head <branch>` from cwd. No regex-scraping PR numbers out of the agent's narrative — that pipeline (deleted 2026-05-26) caused the production bug where unrelated merged PRs drove workspace-close decisions.
- **Observer reads transcripts directly.** No curated turns, no truncation. If it needs the original prompt, it `head -30`s; if it needs to know what tools ran, it scrolls back. The transcript is ground truth.
- **The orchestrator and `/cleanup` both refuse to close workspaces.** That's the user's call. Auto-close is gone.
- **Single sanctioned send path.** Every keystroke goes through `bin/cmux-send.py` so the literal text + post-send transcript-byte delta are logged. Without that, "I sent it" is unverifiable.
- **NO_INGEST_GUARD.** If the last send to a ws returned `transcript_size_delta=0` (cmux returned OK but no claude PID was reading), the orchestrator skips the resend on the next pulse. Breaks the cleanup-loop class of bug structurally.
- **Back-off list.** `~/.assistant/back-off.json` excludes workspaces from every per-ws step. Manageable from inside any cmux workspace via `/back-off` and `/attend` skills.
