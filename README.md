# Assistant

Mukul's personal dispatcher system that manages parallel cmux Claude Code workspaces. Architecture is **3 components + 1 brain**:

| Component | Cadence | Role |
|---|---|---|
| **Scanner** (`bin/world-scanner.py`) | 30s | Reads cmux registry + Claude transcripts, writes `~/.claude/cache/world.json` |
| **Triage** (Sonnet 1M Claude in cmux ws:126) | 2min pulse | Reads `world.json`, decides + acts in one loop, writes `~/.claude/cache/triage-state.json` |
| **Renderer** (`bin/render-assistant-page.py`) | 15s | Renders both files into `~/.claude/assistant-dashboard.html` |
| **TODO server** (`bin/todo-server.py`) | HTTP daemon | Powers dashboard buttons (`/focus/<ws>`, `/toggle`, `/remove`) on `127.0.0.1:9876` |
| **Session-context watcher** (`bin/session-context-watcher.py`) | event-driven (kqueue) | Tails Claude JSONL transcripts → `~/.claude/cache/session-context.json` |

The dispatcher itself (this Claude session) reads `MEMORY.md`, `docs/assistant-operating-guide.md`, and `lessons/active/*.json` on boot.

## Repo layout

```
assistant/
├── bin/                   # All five daemons + curator CLI
├── prompts/               # Triage agent prompt (the policy spec)
├── skills/                # /todo, /cleanup, /cmux, /spawn-claude-workspace
├── lessons/active/        # Curated rules from past corrections (read by dispatcher on boot)
├── evals/                 # Regression tests for Triage policy decisions
├── launchagents/          # macOS LaunchAgent plists for the 5 daemons
└── docs/                  # assistant-operating-guide.md
```

## Where the live system reads from

This repo is the **source of truth**. The actual running system reads from `~/.claude/` and `~/Library/LaunchAgents/`. The two are kept in sync manually for now (no build/install step yet — Mukul will iterate). To deploy a change:

```bash
# bin/
cp bin/world-scanner.py ~/.claude/bin/
launchctl kickstart -k gui/$UID/com.mukuls.world-scanner

# Triage prompt — edits land instantly because triage-pulse.sh re-reads the file each pulse.
# But if you've edited the prompt and Triage is mid-session, push the new policy directly:
cp prompts/prompt-triage-agent.md ~/.claude/spawn-prompts/
cmux send --workspace workspace:126 --surface surface:239 "POLICY UPDATE — re-read $(realpath ~/.claude/spawn-prompts/prompt-triage-agent.md)"
cmux send-key --workspace workspace:126 --surface surface:239 enter

# Skills
cp skills/todo/SKILL.md ~/.claude/skills/todo/

# LaunchAgent
cp launchagents/com.mukuls.world-scanner.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.mukuls.world-scanner.plist
launchctl load   ~/Library/LaunchAgents/com.mukuls.world-scanner.plist
```

## Runtime state — NOT committed

These live under `~/.claude/cache/` and `~/.architect/` and are regenerated continuously:

- `~/.claude/cache/world.json` — Scanner output
- `~/.claude/cache/triage-state.json` — Triage decisions
- `~/.claude/cache/session-context.json` — transcript tails
- `~/.claude/assistant-dashboard.html` — rendered page
- `~/.claude/assistant-todo.json` — TODO data
- `~/.architect/orchestrator-ledger/` — cleanup `--undo` history
- `~/.architect/triage-registry.json` — Triage workspace ref

## Evals

Regression tests for Triage's policy decisions. Run them after any edit to `prompts/prompt-triage-agent.md`:

```bash
python3 evals/triage-cleanup-gating/run.py
```

Each eval spawns a one-shot headless Sonnet 1M pulse against fixture data, captures the decision, and asserts on the resulting `triage-state.json`. PASS exits 0; FAIL prints a diff.

Currently 1 eval:

| Eval | What it locks down |
|---|---|
| `triage-cleanup-gating` | Triage must NOT auto-cleanup a workspace whose PR is OPEN (not MERGED), even when the agent's recap suggests cleanup. Evidence in actions_taken must quote `gh pr view` output, not the agent recap. |

## Adding a new eval

```
evals/<name>/
├── fixtures/world.json         # synthetic world snapshot
├── fixtures/transcript.jsonl   # synthetic Claude transcript
├── fixtures/fake-gh-bin/gh     # optional: mock external CLIs
├── eval-prompt.md              # what the agent should do
└── run.py                      # spawns claude, asserts on output
```

## Component dependency graph

```
                  ┌──────────────────────────┐
                  │ cmux (running workspaces)│
                  └────────────┬─────────────┘
                               │
                ┌──────────────▼──────────────┐
                │  world-scanner (30s tick)   │
                └──────────────┬──────────────┘
                               ▼
                       world.json (cache)
                               │
                ┌──────────────▼──────────────┐         ┌─────────────────────┐
                │  Triage (Sonnet 1M, ws:126) │◄────────┤ triage-pulse (2min) │
                └──────────────┬──────────────┘         └─────────────────────┘
                               ▼
                    triage-state.json (cache)
                               │
                               ▼
                ┌──────────────────────────────┐
                │ render-assistant-page (15s)  │
                └──────────────┬───────────────┘
                               ▼
                  assistant-dashboard.html
```

## Operating guide

See `docs/assistant-operating-guide.md` — that's the runbook this dispatcher reads on every conversation boot.

## Lessons

`lessons/active/*.json` are curated rules from past corrections. The dispatcher checks these before any non-trivial proposal. Write a new lesson via:

```bash
~/.claude/bin/assistant-curator.py write
```

(Curator currently writes to `~/.claude/lessons/active/` — sync into this repo with `cp`.)
