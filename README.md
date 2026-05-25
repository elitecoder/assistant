# Assistant

Mukul's personal dispatcher system that manages parallel cmux Claude Code workspaces. Architecture is **3 components + 1 brain**:

| Component | Cadence | Role |
|---|---|---|
| **Scanner** (`bin/world-scanner.py`) | 30s | Reads cmux registry + Claude transcripts, writes `~/.claude/cache/world.json` |
| **Assistant agent** (Sonnet 1M Claude in a cmux workspace titled "Assistant (Sonnet 1M)") | 2min pulse | Reads `world.json`, decides + acts in one loop, writes `~/.claude/cache/assistant-state.json` |
| **Renderer** (`bin/render-assistant-page.py`) | 15s | Renders both files into `~/.claude/assistant-dashboard.html` |
| **TODO server** (`bin/todo-server.py`) | HTTP daemon | Powers dashboard buttons (`/focus/<ws>`, `/toggle`, `/remove`) on `127.0.0.1:9876` |
| **Session-context watcher** (`bin/session-context-watcher.py`) | event-driven (kqueue) | Tails Claude JSONL transcripts → `~/.claude/cache/session-context.json` |

The dispatcher itself (this Claude session) reads `MEMORY.md`, `docs/assistant-operating-guide.md`, and the `## Lessons` section of `~/.claude/CLAUDE.md` (auto-loaded by Claude Code into every session). Lessons are user-owned runtime state — they live in CLAUDE.md, not in this repo.

## Repo layout

```
assistant/
├── bin/                   # All five daemons + curator CLI + world-observer subagent
├── prompts/               # Assistant agent prompt (the policy spec)
├── skills/                # /todo, /cleanup, /cmux, /spawn-claude-workspace
├── evals/                 # Regression tests for Assistant policy decisions
├── launchagents/          # macOS LaunchAgent plists for the 5 daemons
└── docs/                  # assistant-operating-guide.md
```

## Where the live system reads from

This repo is the **source of truth**. The actual running system reads from `~/.claude/` and `~/Library/LaunchAgents/`. Wire-up is via `install.sh`:

```bash
./install.sh              # dry-run, shows what would change
./install.sh --apply      # actually wires it up
```

After `--apply`:

| Repo path | Live path | Mechanism |
|---|---|---|
| `bin/` | `~/.claude/bin` | symlink |
| `prompts/prompt-assistant-agent.md` | `~/.claude/spawn-prompts/prompt-assistant-agent.md` | symlink |
| `docs/assistant-operating-guide.md` | `~/.claude/assistant-operating-guide.md` | symlink |
| `skills/todo/`, `skills/cleanup/`, `skills/spawn-claude-workspace/` | `~/.claude/skills/<name>/` | **copy** (so they're shareable) |
| `launchagents/*.plist` | `~/Library/LaunchAgents/*.plist` | copy + launchctl reload |

### Why some are symlinks and skills are copies

- **Code/prompts/lessons are symlinked** so an edit in the repo is live immediately — no copy step.
- **Skills are copied** so each skill directory is a self-contained, shareable artifact. You can `cp -r ~/dev/assistant/skills/todo /elsewhere/` and it just works. The trade-off: edits made in place at `~/.claude/skills/<name>/` don't auto-sync back. Use `./install.sh --pull-skills` to bring those edits into the repo (it shows a diff in dry-run, applies on `--apply`).
- **Plists are copied** because launchd does not follow symlinks reliably.

### Daily workflow

```bash
# Edit code in the repo — it's live (symlink).
$EDITOR bin/world-scanner.py
launchctl kickstart -k gui/$UID/com.assistant.world-scanner   # reload that one daemon

# Edit Assistant prompt — re-read by assistant-pulse.sh each pulse, no daemon reload needed.
# But if the Assistant is mid-session, push the policy update directly:
$EDITOR prompts/prompt-assistant-agent.md
WS=$(python3 -c 'import json; print(json.load(open("/Users/mukuls/.assistant/heartbeat.json"))["ws_ref"])')
cmux send --workspace "$WS" "POLICY UPDATE — re-read prompt"
cmux send-key --workspace "$WS" Return

# Edit a skill in the repo — re-run install to copy it live.
$EDITOR skills/todo/SKILL.md
./install.sh --apply

# Edit a skill IN PLACE at ~/.claude/skills/todo/ then pull the edit back.
$EDITOR ~/.claude/skills/todo/SKILL.md
./install.sh --pull-skills          # dry-run; shows the diff
./install.sh --pull-skills --apply  # write into the repo
git diff skills/todo                # review
git commit -am "..."

# Edit a plist — re-run install to copy + reload that daemon.
$EDITOR launchagents/com.assistant.world-scanner.plist
./install.sh --apply
```

## Runtime state — NOT committed

These live under `~/.claude/cache/` and `~/.architect/` and are regenerated continuously:

- `~/.claude/cache/world.json` — Scanner output
- `~/.claude/cache/assistant-state.json` — Assistant decisions
- `~/.claude/cache/session-context.json` — transcript tails
- `~/.claude/assistant-dashboard.html` — rendered page
- `~/.claude/assistant-todo.json` — TODO data
- `~/.architect/orchestrator-ledger/` — cleanup `--undo` history
- `~/.assistant/heartbeat.json` — Assistant workspace ref (self-healing; written each pulse)

## Evals

Regression tests for the Assistant's policy decisions. Run them after any edit to `prompts/prompt-assistant-agent.md`:

```bash
python3 evals/assistant-cleanup-gating/run.py
```

Each eval spawns a one-shot headless Sonnet 1M pulse against fixture data, captures the decision, and asserts on the resulting `assistant-state.json`. PASS exits 0; FAIL prints a diff.

Currently 3 evals:

| Eval | What it locks down |
|---|---|
| `assistant-cleanup-gating` | Assistant must NOT auto-cleanup a workspace whose PR is OPEN (not MERGED), even when the agent's recap suggests cleanup. Evidence in actions_taken must quote `gh pr view` output, not the agent recap. |
| `assistant-inflight-check` | Assistant must skip dispatch when the same work is already in flight in another workspace. |
| `assistant-stale-card-purge` | Assistant must drop carried-over awaiting cards whose predicate is no longer true, and dispatch any newly-eligible TODOs in the same pulse. |

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
                ┌──────────────▼──────────────┐         ┌────────────────────────┐
                │  Assistant (Sonnet 1M, ws:N) │◄────────┤ assistant-pulse (2min) │
                └──────────────┬──────────────┘         └────────────────────────┘
                               ▼
                  assistant-state.json (cache)
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

Lessons are rules. They live inside `~/.claude/CLAUDE.md` under a `## Lessons` heading, in this format:

```markdown
<!-- lesson: <slug>, scope: <scope>, added: <YYYY-MM-DD> -->
**<one-line trigger>** <rule body, one paragraph>
```

CLAUDE.md is auto-loaded by Claude Code into every session, so any agent — this Assistant, an ad-hoc claude session, or the per-workspace observer subagents — sees these rules with no extra wiring. The repo doesn't track lessons; each install grows its own.

Write a new lesson:

```bash
~/.claude/bin/assistant-curator.py write \
  --trigger "<one-line situation>" \
  --rule "<what to do or not do>" \
  --scope "global|dispatch|dashboard|todo|ffp|security|..."
```

Other subcommands: `list`, `rm <slug>`, `trim` (opens CLAUDE.md in `$EDITOR`).

The dispatcher delegates observation + decisioning to a fresh **per-workspace observer subagent** (`bin/world-observer-subagent.py`) that fans out one Sonnet call per workspace in parallel. Each per-ws call has CLAUDE.md auto-loaded (lessons + global rules) plus the `## Assistant policies` excerpt from the prompt — so rules stay at the top of attention rather than buried in pulse history. The observer's `proposed_actions` are authorized to execute directly; there is no separate judgement pass.
