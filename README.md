<div align="center">

# 🛰️ Assistant

**A mechanical orchestrator for a fleet of parallel Claude Code workspaces.**

*One LLM call per pulse — for judgment only. Everything else is Python you can unit-test.*

<br/>

[![orchestration](https://img.shields.io/badge/orchestration-zero%20LLM-2ea44f?style=flat-square)](#-design-notes)
[![observer](https://img.shields.io/badge/observer-1%20LLM%20call%20%2F%20pulse-3b82f6?style=flat-square)](#-control-loop)
[![python](https://img.shields.io/badge/python-3.x-3776AB?style=flat-square&logo=python&logoColor=white)](#-testing)
[![platform](https://img.shields.io/badge/platform-macOS-lightgrey?style=flat-square&logo=apple)](#-where-the-live-system-reads-from)
[![daemons](https://img.shields.io/badge/LaunchAgents-8-f97316?style=flat-square)](#-components)
[![evals](https://img.shields.io/badge/observer%20evals-13%20fixtures-8b5cf6?style=flat-square)](#observer-eval-llm-real-fixtures)

</div>

---

You start a dozen Claude Code workspaces in [cmux](https://github.com/cmuxterm/cmux) and walk away. **Assistant watches them for you.** Every five minutes it reads each workspace's transcript, asks one Observer LLM "what state is this in?", and dispatches the right slash command — merge the PR, clean up the worktree, nudge a stalled agent, or flag the ones that genuinely need *you*.

The orchestration layer that decides *what to do* has **no LLM in it at all**. The only model call is the Observer, and its only job is to emit a verdict. Turning a verdict into an action is a Python dictionary, not a prompt the model can drift away from. That's the whole design: **judgment is fuzzy and lives in one auditable place; everything mechanical is code you can pin with a unit test.**

## ✨ Why it's powerful

- **🤖 Hands-off fleet management.** Spin up N parallel agents, close the laptop. Merges, cleanups, and nudges happen on a 5-minute pulse without you in the loop.
- **🧠 LLM isolated to a single decision.** The model judges; Python acts. Verdict → action is a lookup table — no prompt-injection of behavior, no drift, no "the model decided to close my workspace."
- **🛟 Structurally can't run away.** The orchestrator *never* closes a workspace. A `NO_INGEST_GUARD` kills resend loops. A back-off list excludes any workspace from every step. Safety is in the control flow, not in a careful prompt.
- **🔍 Every action is auditable forever.** Each Observer call, every keystroke sent, and every dispatched action lands on disk with a `verified_via` proof field. "I sent it" is never unverifiable.
- **🧪 Behavior is testable.** Bugs are diffs + unit tests, not prompt rewrites and restart cycles. 13 real-transcript fixtures pin the Observer; a Python suite pins the mechanics.

> [!NOTE]
> The orchestrator never closes cmux workspaces — that's the operator's job. Slash commands run **inside** each workspace and own their own state (branch, PR number, TODO id). Assistant only ever *sends* commands; it never reaches in and mutates a workspace's git or filesystem.

## 🔁 Control loop

`bin/pulse.py` runs as the `com.assistant.assistant-pulse` LaunchAgent every 5 minutes. Each pulse it batches up to `WS_BATCH_SIZE=10` workspaces per Observer subprocess (in parallel), spawns one `claude --print` session per batch with the `observer-batch-prompt.md` ruleset, reads each batch's `verdicts.jsonl` **from disk**, and dispatches the matching slash command per workspace via a fixed lookup table.

```
┌─────────────────────────────────────────────────────────────────┐
│  com.assistant.assistant-pulse LaunchAgent (every 5 min)         │
│   → /opt/homebrew/bin/python3 bin/pulse.py                       │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  pulse.py — mechanical orchestrator (no LLM)                     │
│   1. Drain ~/.assistant/inbox/                                   │
│   2. Run bin/purge-stale-awaiting.py                             │
│   3. bin/pick-ws-batch.py → to_reclassify[], backed_off[]        │
│      (back-off filter applied here, upstream of every step)      │
│   4. Build ctx for each ws via bin/build-ws-context.py           │
│   5. Chunk into batches of WS_BATCH_SIZE=10                      │
│   6. Spawn ceil(N/10) Observer subprocesses in parallel          │
│        ThreadPoolExecutor → claude --print → verdicts.jsonl      │
│   7. Read each batch's verdicts.jsonl from disk                  │
│   8. Per ws: save-ws-summary.py + execute via lookup table:      │
│        ready_for_merge   → cmux-send /merge-when-ready           │
│        ready_for_cleanup → cmux-send /cleanup                    │
│        stranded          → cmux-send <nudge_text>                │
│        needs_user        → append awaiting_input[]               │
│        active            → no-op                                 │
│        no_action         → no-op (cleanup already ran)           │
│   9. NO_INGEST_GUARD — skip resends if last send was delta=0     │
│   10. state-write.py → assistant-state.json + per-pulse trace    │
│   11. heartbeat.json (drives dashboard health banner)            │
└────────────────────────────┬─────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Observer subprocess (one per batch of ≤10 workspaces)           │
│   claude --print --dangerously-skip-permissions                  │
│   stdin = observer-batch-prompt.md + ctx JSON for batch          │
│   Reads each transcript via bash (tail -200 ...).                │
│   Writes JSONL to verdicts.jsonl (file, not stdout).             │
│   Stdout/stderr captured to observer-runs/ for audit.            │
└─────────────────────────────────────────────────────────────────┘
```

## 🧩 Components

| File | Cadence | Role |
|---|---|---|
| `bin/pulse.py` | LaunchAgent every 5 min | Mechanical orchestrator: pick → ctx → Observer batch → execute → trace |
| `prompts/observer-batch-prompt.md` | re-read per batch | Observer ruleset; outputs JSONL keyed by ws_ref |
| `bin/build-ws-context.py` | called per ws per pulse | Builds the JSON ctx Observer reads |
| `bin/purge-stale-awaiting.py` | called by pulse step 2 | Drops awaiting cards whose underlying state changed |
| `bin/pick-ws-batch.py` | called by pulse step 3 | Returns `to_reclassify[]` / `backed_off[]`; reads back-off.json |
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
| `bin/merge-pr-dispatch.py` | called by `/merge-when-ready` skill | Safety-gated PR merge dispatcher |

### 📱 assistant-comms — text-channel watcher over Assistant

A **second Claude session in Terminal.app** (not cmux) that watches Assistant and converses with you over Telegram. It reports Assistant's actions, pages you when Assistant's heartbeat goes stale, and gives you a recovery surface (`lesson` / `restart` / `respawn`) — all gated on a human `y`. It **reads** Assistant's state, never mutates it except through three confirmed CLIs.

| File | Role |
|---|---|
| `prompts/prompt-assistant-comms-agent.md` | Boot prompt: 5-step pulse, conversation-first inbound, confirm-back on mutations |
| `bin/comms_lib.py` | Pure helpers — Paths, Config, formatting, ledger/TG cursors, threads, **conversation memory**, comms heartbeat |
| `bin/tg-send.py` / `bin/tg-poll.py` | Telegram send (threaded replies, mute-aware) / long-poll inbound (stdlib urllib) |
| `bin/conversation.py` | Durable chat memory (`conversation.jsonl`) — `append` a turn, `window` to rebuild recent thread |
| `bin/link-msg.py` / `bin/lookup-thread.py` | Link a sent message to the ledger entry it reported on / resolve a reply back to it |
| `bin/spawn-comms.sh` | Opens the Terminal.app window, launches `claude` with the boot prompt, records the tab id |
| `bin/assistant-comms-setup.sh` | One-time: BotFather token + chat_id capture → `~/.assistant/comms/config.json` (chmod 600) |
| `launchagents/com.assistant.assistant-comms-spawn.plist` | RunAtLoad + crash-only KeepAlive; respawns the Terminal session |

**Disposable context by design:** the comms session treats its context window as scratch. Every turn it reconstructs the recent thread from `conversation.jsonl` (bounded to the last 20 turns / 2h), reasons, replies, and writes both turns back. A crash, `/clear`, or auto-compact loses nothing. Setup + troubleshooting: [`docs/assistant-comms-onboarding.md`](docs/assistant-comms-onboarding.md).

> [!TIP]
> **Companion service:** [`slack-reactor/`](slack-reactor/README.md) lets you react with this machine's emoji on any Slack thread to capture the whole thread as a `/todo` — which the pulse then auto-dispatches into a fresh workspace.

## 📂 Repo layout

```
assistant/
├── bin/                      # daemons, helpers, the per-pulse pipeline (32 scripts)
├── prompts/
│   └── observer-batch-prompt.md  # the only LLM prompt in the loop
├── skills/                   # /todo, /cleanup, /spawn-claude-workspace, /back-off, /attend, /lesson
├── evals/observer/           # 13 real-transcript fixtures × Sonnet → verdict assertions
├── tests/                    # Python unit tests (no LLM)
├── slack-reactor/            # companion: Slack emoji react → /todo
├── launchagents/             # 8 macOS LaunchAgent plists
└── docs/                     # operating guide
```

## 🔌 Where the live system reads from

This repo is the source of truth. The running system reads from `~/.claude/` and `~/Library/LaunchAgents/`. Wire it up with `install.sh`:

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

## 🧾 Observer audit trail

Every Observer call leaves a permanent on-disk record at `~/.assistant/observer-runs/<pulse_idx>/batch-<batch_idx>/`:

```
prompt.md         # full prompt sent to claude
ctxs.json         # input ctxs we asked it to judge
verdicts.jsonl    # claude wrote this — orchestrator reads ONLY this for verdicts
stdout.txt        # claude's --print stdout (work trail: tool calls, reasoning)
stderr.txt
meta.json         # rc, wall_ms, model, cmd, ws_refs, ts
```

> [!IMPORTANT]
> These records are **never deleted**. Disk is cheap; an audit trail is not. If the orchestrator ever does something surprising, the exact prompt, inputs, and model output that produced the decision are still on disk.

## 🧪 Testing

### Python unit tests (no LLM)

```bash
python3 -m unittest discover tests -v        # ~8s
```

| File | Pins |
|---|---|
| `test_pulse.py` | inbox drain, JSONL parse, verdict→action lookup, NO_INGEST_GUARD, chunker |
| `test_back_off.py` / `test_back_off_in_process.py` | back-off list filter is applied; CLI add/remove/list |
| `test_purge_stale_awaiting.py` | drop predicates: closed workspaces, done TODOs, cmux-down safety |
| `test_build_ws_context.py` / `…_in_process.py` | mechanical signals; no PR data leaks into output |
| `test_pickers_in_process.py` | `pick-ws-batch`, `pick-open-todos` selection logic |
| `test_save_summary_in_process.py` | verdict persistence; rejects verdicts missing `next` |
| `test_send_dispatch_in_process.py` | `cmux-send`, `merge-pr-dispatch`, `cmux-ws-numberer` — never call cmux/gh for real |
| `test_writers_in_process.py` | `state-write`, actions-ledger, transcript-tail, curator |
| `test_renderer_in_process.py` | dashboard HTML renderer |
| `test_comms_lib.py` / `test_tg_poll.py` / `test_tg_send.py` | comms helpers + Telegram poll/send |
| `test_conversation.py` / `test_threading_tools.py` | comms durable chat memory (append/window bounds); message↔ledger threading |
| `test_no_close_workspace.py` | **regression-pin:** no production code path shells out `cmux close-workspace` |

### Observer eval (LLM, real fixtures)

```bash
cd evals/observer
./run.py                                   # all 13 fixtures
./run.py 01-ws97-trap-no-pr-mid-audit      # one fixture
EVAL_MODEL=us.anthropic.claude-sonnet-4-6[1m] ./run.py
```

Each fixture under `fixtures/<case>/` has a real or synthesized session `transcript.jsonl`, the `ctx.json` `build-ws-context.py` would emit, an `expected.json`, and an optional `fake-gh-bin/gh` shim mocking `gh pr view`. Fixtures may declare `forbidden_verdicts` — if the Observer emits one, the runner reports `DANGEROUS:` rather than just a verdict mismatch.

> [!WARNING]
> The headline regression-pin (fixture `01-ws97-trap-no-pr-mid-audit`) replays the production-bug transcript: an agent 6 of 14 specs into a phonebook-retirement audit, whose prose happens to mention an *unrelated* merged PR. The old pipeline scraped that PR number and auto-closed the workspace. The new pipeline must emit `active`. **Run the eval after any change to `prompts/observer-batch-prompt.md`, `bin/build-ws-context.py`, or any verdict-execution path.**

## 🛠️ Daily workflow

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

## 💾 Runtime state — NOT committed

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
- `~/.assistant/comms/config.json` — comms bot token + chat_ids (chmod 600; **never committed**)
- `~/.assistant/comms/conversation.jsonl` — comms durable chat memory (rebuilt into context each turn)
- `~/.assistant/comms/{threads.jsonl,ledger.cursor,tg.cursor,heartbeat.json}` — comms threading + cursors + own heartbeat

## 📓 Lessons

Rules learned from past incidents live inside `~/.claude/CLAUDE.md` under a `## Lessons` heading. CLAUDE.md is auto-loaded by Claude Code into every session, so the rules apply to ad-hoc Claude calls without extra wiring. The repo doesn't track lessons.

```bash
~/.claude/bin/assistant-curator.py write \
  --trigger "<one-line situation>" \
  --rule "<what to do or not do>" \
  --scope "global|classification|dashboard|ffp|scout|memory|security"
```

Subcommands: `list`, `rm <slug>`, `trim` (opens CLAUDE.md in `$EDITOR`).

## 🎯 Design notes

- **Mechanical orchestrator, isolated LLM.** The Observer prompt is the only LLM in the loop and its only job is judgment. Verdict→action is a Python dict, not a prompt example the LLM can drift from. Bugs are diffs + unit tests, not prompt rewrites + restart cycles.
- **PR data is fetched by branch, not prose.** `gh pr view --head <branch>` from cwd. No regex-scraping PR numbers out of the agent's narrative — that pipeline (deleted 2026-05-26) caused the production bug where unrelated merged PRs drove workspace-close decisions.
- **Observer reads transcripts directly.** No curated turns, no truncation. If it needs the original prompt, it `head -30`s; if it needs to know what tools ran, it scrolls back.
- **A wrong transcript is worse than none.** `build-ws-context.py` attaches `transcript_path` ONLY when it is *verified* to belong to the workspace's live agent — never guessed. It finds the Claude pane by enumerating every pane (a split workspace's focused pane is often a shell), reads the session id from the agent's own status bar (`… │ #<8hex>`, not from `#<8hex>` in conversation text), and confirms the file self-identifies as that session. The cmux registry is a fallback, trusted only when its `claude_pid` is still alive and it agrees with the live screen (the surface→session map goes stale on surface reuse). No verified signal ⇒ `transcript_path: null` and the Observer judges from `screen_text` alone. The old mtime/cwd/title heuristic that misattributed transcripts was deleted (2026-06-05; ws:12 attached a dead headless-pulse's session, ws:5 self-matched a session id it was merely *discussing*).
- **`screen_text` is the agent pane's live terminal.** The Observer trusts it over transcript-derived signals on conflict — it's what the agent is showing *right now*. `screen_shows_error` (an `⏺ API Error:`-shaped halt banner) is read from the agent pane only, so an error printed in a sibling shell pane can't trigger a spurious `stranded` nudge.
- **The orchestrator and `/cleanup` both refuse to close workspaces.** That's the user's call. Auto-close is gone.
- **Single sanctioned send path.** Every keystroke goes through `bin/cmux-send.py` so the literal text + post-send transcript-byte delta are logged. Without that, "I sent it" is unverifiable.
- **NO_INGEST_GUARD.** If the last send to a ws returned `transcript_size_delta=0` (cmux returned OK but no claude PID was reading), the orchestrator skips the resend on the next pulse. Breaks the cleanup-loop class of bug structurally.
- **Back-off list.** `~/.assistant/back-off.json` excludes workspaces from every per-ws step. Manageable from inside any cmux workspace via the `/back-off` and `/attend` skills.

<div align="center">
<sub>Mukul's personal dispatcher for parallel cmux Claude Code workspaces · no LLM in orchestration · everything auditable</sub>
</div>
