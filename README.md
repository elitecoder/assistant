<div align="center">

# 🛰️ Assistant

**A mechanical fleet manager for a swarm of parallel Claude Code workspaces — with memory, learning, and event-driven delivery.**

*One LLM call per pulse — for judgment only. Everything else is Python you can unit-test.*

<br/>

[![orchestration](https://img.shields.io/badge/orchestration-zero%20LLM-2ea44f?style=flat-square)](#-design-notes)
[![observer](https://img.shields.io/badge/observer-1%20LLM%20call%20%2F%20pulse-3b82f6?style=flat-square)](#-control-loop)
[![python](https://img.shields.io/badge/python-3.x-3776AB?style=flat-square&logo=python&logoColor=white)](#-testing)
[![platform](https://img.shields.io/badge/platform-macOS-lightgrey?style=flat-square&logo=apple)](#-where-the-live-system-reads-from)
[![daemons](https://img.shields.io/badge/LaunchAgents-8-f97316?style=flat-square)](#-components)
[![memory](https://img.shields.io/badge/memory-108%20mem0%20%2B%2053%20vault-06b6d4?style=flat-square)](#-memory--learning)
[![evals](https://img.shields.io/badge/observer%20evals-13%20fixtures-8b5cf6?style=flat-square)](#observer-eval-llm-real-fixtures)

</div>

---

You start a dozen Claude Code workspaces in [cmux](https://github.com/cmuxterm/cmux) and walk away. **Assistant watches them for you.** Every five minutes it reads each workspace's transcript, asks one Observer LLM "what state is this in?", and dispatches the right slash command — merge the PR, clean up the worktree, nudge a stalled agent, or flag the ones that genuinely need *you*.

What began as a workspace dispatcher has grown into a **personal AI fleet manager**: it texts you from your phone, pings you the instant a workspace needs input, mines your own sessions for rules worth keeping, and carries a cross-machine memory of how you like to work — all on the same mechanical spine.

The orchestration layer that decides *what to do* has **no LLM in it at all**. The only model call is the Observer, and its only job is to emit a verdict. Turning a verdict into an action is a Python dictionary, not a prompt the model can drift away from. That's the whole design: **judgment is fuzzy and lives in one auditable place; everything mechanical is code you can pin with a unit test.**

## ✨ Why it's powerful

- **🤖 Hands-off fleet management.** Spin up N parallel agents, close the laptop. Merges, cleanups, and nudges happen on a 5-minute pulse without you in the loop.
- **🧠 LLM isolated to a single decision.** The model judges; Python acts. Verdict → action is a lookup table — no prompt-injection of behavior, no drift, no "the model decided to close my workspace."
- **📱 In your pocket.** Text Assistant from Telegram; a warm Claude session answers in ~2.6s, grounded in live fleet state. It pings you on every verified action and pages you if the heartbeat goes stale.
- **📚 It learns your rules.** A lesson extractor mines the action ledger *and* your real session transcripts for corrections, confirmations, and recurring questions, drafts a rule, and pings you to confirm — then routes it to the right store and syncs it across machines.
- **🛟 Structurally can't run away.** The orchestrator *never* closes a workspace. A `NO_INGEST_GUARD` kills resend loops. A back-off list excludes any workspace from every step. A receipt gate blocks `/cleanup` on undocumented work. Safety is in the control flow, not in a careful prompt.
- **🔍 Every action is auditable forever.** Each Observer call, every keystroke sent, and every dispatched action lands on disk with a `verified_via` proof field. "I sent it" is never unverifiable.
- **🧪 Behavior is testable.** Bugs are diffs + unit tests, not prompt rewrites and restart cycles. 13 real-transcript fixtures pin the Observer; a 29-file Python suite pins the mechanics.

> [!NOTE]
> The orchestrator never closes cmux workspaces — that's the operator's job. Slash commands run **inside** each workspace and own their own state (branch, PR number, TODO id). Assistant only ever *sends* commands; it never reaches in and mutates a workspace's git or filesystem.

## 🔁 Control loop

`bin/pulse.py` runs as the `com.assistant.assistant-pulse` LaunchAgent every 5 minutes. Each pulse it self-updates (throttled hourly), batches up to `WS_BATCH_SIZE=10` workspaces per Observer subprocess (in parallel), spawns one `claude --print` session per batch with the `observer-batch-prompt.md` ruleset, reads each batch's `verdicts.jsonl` **from disk**, dispatches the matching slash command per workspace via a fixed lookup table, and (every ~12th pulse) runs the lesson extractor.

```
   PHONE  ◀──────────────────── Telegram ────────────────────▶  comms-listen.py
     ▲                                                          (warm Claude session,
     │  pings + warm replies                                     event-driven daemon)
     │                                                                   ▲
┌────┴──────────────────────────────────────────────────────────┐      │ inbox drop
│  com.assistant.assistant-pulse LaunchAgent (every 5 min)        │      │ (~seconds)
│   → python3 bin/pulse.py                                        │  ┌───┴────────────┐
└────────────────────────────┬────────────────────────────────── ┘  │ cmux-watcher   │
                             ▼                                        │ taps cmux event│
┌─────────────────────────────────────────────────────────────────┐ │ stream, matches│
│  pulse.py — mechanical orchestrator (no LLM)                     │ │ pattern_bank   │
│   0. self_update.py    — git pull --ff-only (hourly throttle)    │ └────────────────┘
│   1. Drain ~/.assistant/inbox/                                  │
│   2. purge-stale-awaiting.py                                    │
│   3. pick-ws-batch.py → to_reclassify[], backed_off[]           │
│   4. build-ws-context.py per ws                                 │
│   5. Chunk into batches of WS_BATCH_SIZE=10                     │
│   6. Spawn ceil(N/10) Observer subprocesses in parallel ────────┼──┐
│   7. Read each batch's verdicts.jsonl from disk                 │  │
│   8. Per ws: save-ws-summary.py + execute via lookup table:     │  │
│        ready_for_merge   → cmux-send /merge-when-ready          │  │
│        ready_for_cleanup → pre-cleanup-check.py → /cleanup      │  │
│        stranded          → cmux-send <nudge_text>               │  │
│        needs_user        → append awaiting_input[]              │  │
│        active / no_action → no-op                               │  │
│   9. NO_INGEST_GUARD — skip resends if last send was delta=0    │  │
│  10. state-write.py → assistant-state.json + actions-ledger ────┼──┼──▶ comms-listen
│  11. heartbeat.json (drives dashboard health banner)            │  │     pings phone
│  12. run_lesson_extractor.py (every ~12th pulse) ───────────────┼──┼──▶ proposals.jsonl
└─────────────────────────────────────────────────────────────────┘  │
                                                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  Observer subprocess (one per batch of ≤10 workspaces)           │
│   claude --print --dangerously-skip-permissions                  │
│   stdin = observer-batch-prompt.md (+ ## Lessons) + ctx JSON     │
│   Reads each transcript via bash (tail -200 ...).                │
│   Writes JSONL to verdicts.jsonl (file, not stdout).             │
└─────────────────────────────────────────────────────────────────┘

         ┌──── memory & learning (alongside the loop) ────────────────────┐
         │  ~/.claude/CLAUDE.md   ## Lessons   (every session)            │
         │  prompts/observer-batch-prompt.md   ## Lessons (Observer only)  │
         │  <repo>/.claude/rules/*.md          (project-scoped, auto-commit)│
         │  ~/.assistant/mem0/      108 semantic memories (5 categories)   │
         │  ~/dev/obs-elitecoder/   53 Obsidian notes                      │
         │            └──── all synced to the private mukul-memory repo ───┘
```

## 🧩 Components

| File | Cadence | Role |
|---|---|---|
| `bin/pulse.py` | LaunchAgent every 5 min | Mechanical orchestrator: self-update → pick → ctx → Observer batch → execute → trace → extract |
| `prompts/observer-batch-prompt.md` | re-read per batch | Observer ruleset (+ `## Lessons`); outputs JSONL keyed by ws_ref |
| `bin/self_update.py` | pulse step 0 (hourly) | `git pull --ff-only`; refuses a dirty/ahead tree; runs `install.sh` only when copied artifacts change |
| `bin/build-ws-context.py` | called per ws per pulse | Builds the JSON ctx Observer reads |
| `bin/purge-stale-awaiting.py` | pulse step 2 | Drops awaiting cards whose underlying state changed |
| `bin/pick-ws-batch.py` | pulse step 3 | Returns `to_reclassify[]` / `backed_off[]`; reads back-off.json |
| `bin/save-ws-summary.py` | per verdict | Persists per-ws verdict; rejects verdicts missing `next` |
| `bin/cmux-send.py` | per send action | Single sanctioned send path; logs literal text + transcript-byte delta |
| `bin/pre-cleanup-check.py` | before `/cleanup` | Gate: blocks auto-cleanup unless a work receipt exists for the ws |
| `bin/state-write.py` | end of pulse | Atomically writes assistant-state.json + per-pulse trace |
| `bin/lesson-extractor.py` | every ~12th pulse | Mines ledger + session transcripts for rules; drafts proposals; pings you |
| `bin/cmux-watcher.py` | event-driven (opt-in) | Taps `cmux events`; pattern-matches terminal output; instant inbox delivery |
| `bin/back-off.py` | CLI (`/back-off`, `/attend`) | Manages workspaces the orchestrator must skip |
| `bin/render-assistant-page.py` | LaunchAgent every 15s | Dashboard HTML (Workspaces + **Fleet kanban** + TODO tabs); pulse-health banner |
| `bin/todo-server.py` | HTTP daemon :9876 | Powers dashboard buttons |
| `bin/world-scanner.py` | LaunchAgent every 30s | Builds the world.json snapshot the dashboard reads |
| `bin/session-context-watcher.py` | event-driven (kqueue) | Tails Claude JSONL transcripts |
| `bin/workspace-watcher.py` | LaunchAgent | Detects cmux workspace lifecycle events |
| `bin/cmux-ws-numberer.py` | event-driven (cmux events) | Appends `[N]` workspace-ref suffix to every cmux workspace title |
| `bin/assistant-curator.py` | CLI | Writes lessons to one of 5 stores; auto-commits project stores; audits misrouting |
| `bin/tool-dispatch.py` | CLI / daemon | Named-tool dispatcher over `bin/tools-manifest.json` (see [Tool registry](#-tool-registry)) |
| `bin/merge-pr-dispatch.py` | called by `/merge-when-ready` | Safety-gated PR merge dispatcher |
| `src/assistant/` | opt-in daemon | Single-process daemon (pulse + comms + heartbeat + tools in one binary) — see [below](#-single-process-daemon-v2) |

### 📱 assistant-comms — event-driven text channel over Assistant

Text Assistant from your phone. An **event-driven daemon** (`comms-listen.py`, a KeepAlive LaunchAgent) bridges Telegram to a **warm cmux Claude session** that answers as your assistant — grounded in Assistant's live state, conversationally, in **~2.6s**. It also pings you when Assistant takes a verified action and pages you if Assistant's heartbeat goes stale. Mutations to Assistant (`lesson` / `restart` / `respawn`) are gated on a human `y`.

**Three event loops, no polling-for-the-sake-of-it:**
- **Inbound** — Telegram long-poll (`getUpdates`) returns the instant you message (no 5-min queue). The daemon feeds it to the warm session, which replies in seconds.
- **Outbound pings** — watches `actions-ledger.jsonl`; formats + sends each new verified/failed action. No LLM, ~4s.
- **Heartbeat page** — 60s stale-check, templated, dedup'd. No LLM.

| File | Role |
|---|---|
| `bin/comms-listen.py` | The daemon: long-poll inbound + ledger watch + heartbeat timer + inbox-drop kqueue. Singleton via `flock`. |
| `bin/comms_session.py` | Warm cmux session lifecycle: spawn (Sonnet, scoped `--add-dir`), feed, read reply, **clear-and-resume at 50% context**, no-leak respawn. |
| `prompts/prompt-assistant-comms-warm.md` | The warm session's identity — your assistant; calls named tools; proposes-then-confirms mutations; survives a clear-and-resume. |
| `bin/comms_lib.py` | Pure helpers — Paths, Config, formatting, ledger/TG cursors, threads, conversation memory, context-token measure, Bedrock env. |
| `bin/tg-send.py` / `bin/tg-poll.py` | Telegram send (threaded, mute-aware) / long-poll inbound (stdlib urllib). |
| `bin/conversation.py` | Durable chat memory (`conversation.jsonl`) — `append` a turn, `window` to rebuild recent thread. |
| `bin/link-msg.py` / `bin/lookup-thread.py` | Link a sent message to the ledger entry it reported on / resolve a reply back to it. |
| `launchagents/com.assistant.assistant-comms.plist` | KeepAlive daemon (listens; no `StartInterval`). |

**Warm but disposable:** the session stays hot for fast replies, but its memory is `conversation.jsonl`, never its context window. At 50% of the 1M window the daemon **clears-and-resumes** it — re-reads its boot prompt for identity, reconstructs the thread from disk — so a long conversation never bloats and a crash loses nothing. **Scoped writes:** the session can edit its *own* surface (`~/dev/assistant`) — intended self-improvement — but not `~/.claude` global rules; it can't widen its own permissions. Setup + troubleshooting: [`docs/assistant-comms-onboarding.md`](docs/assistant-comms-onboarding.md).

> [!TIP]
> **Companion service:** [`slack-reactor/`](slack-reactor/README.md) lets you react with this machine's emoji on any Slack thread to capture the whole thread as a `/todo` — which the pulse then auto-dispatches into a fresh workspace.

## 📡 Event-driven delivery — cmux-watcher

The pulse is a 5-minute heartbeat; some signals shouldn't wait that long. `bin/cmux-watcher.py` subscribes to `cmux events --category agent --reconnect` and turns two classes of Claude-Code lifecycle event into inbox items `comms-listen.py` pings to your phone within **seconds**:

- **`needs_input`** — `agent.hook.Notification` / `agent.hook.AskUserQuestion`: the agent is blocked on you. Always dropped (subject to a cooldown).
- **`work_complete`** — `agent.hook.Stop` (turn end): the live terminal is read and matched against `pattern_bank.json`. An item is dropped **only** when a non-muted pattern fires (PR opened, CI green/red, awaiting review, …). A bare turn-end is the noise floor — never pinged.

The pattern bank **hot-reloads** on file change (lazy mtime check, no poll loop) and patterns carry a `priority`; setting it to `muted` silences a pattern without deleting it. Two learning loops close around the watcher, both wired through the lesson extractor:

1. **Noise feedback** — a transcript correction about pinging ("stop sending me X", "that wasn't worth it") routes to `bin/tools/pattern-feedback.py`, which downgrades the implicated pattern toward `muted`.
2. **Pattern discovery** (`lesson-extractor.py --discover`) — scans terminal snippets that *preceded* `needs_user` ledger entries for recurring phrases no current pattern catches, and proposes them as new patterns for you to confirm.

> [!IMPORTANT]
> The cmux-watcher is **opt-in**. `install.sh` writes its LaunchAgent plist but never loads it (per the "ask before `launchctl load`" rule). Activate it by hand:
> `launchctl load ~/Library/LaunchAgents/com.mukul.assistant-cmux-watcher.plist`.

## 🧠 Memory & learning

Assistant remembers across pulses, across sessions, and across machines. Three layers, one private sync repo.

### Lessons — detect → propose → confirm → route → sync

A lesson is a rule. They flow through one pipeline regardless of where they originate:

1. **Detect.** `lesson-extractor.py` runs every ~12th pulse. **Pass 1** mines `actions-ledger.jsonl` for a verdict + evidence shape repeated ≥3× in 72h. **Pass 2** scans your real Claude session transcripts (`~/.claude/projects/*/*.jsonl`) for corrections ("no, don't…", "you keep…"), confirmations ("perfect", "exactly"), and questions asked across ≥3 distinct sessions. Both passes dedup against existing lessons + pending proposals (idempotent).
2. **Draft.** A one-shot `claude -p` (Sonnet) turns each candidate into `{trigger, rule, target, scope}`.
3. **Propose.** The draft is appended to `proposals.jsonl` (`status: pending`) and you get a Telegram ping.
4. **Confirm.** You reply `y`. The warm comms session runs `tool-dispatch.py propose_lesson --confirm <id>`, which calls the curator and atomically flips the proposal to `confirmed`. (You can also `/lesson` one by hand.)
5. **Route.** `assistant-curator.py` writes the rule to the **correct store** (below) and auto-commits the project stores.
6. **Sync.** A CLAUDE.md lesson fire-and-forget pushes to the cross-machine memory repo.

**Five lesson stores, picked by `--target`:**

| Target | Store | Who loads it |
|---|---|---|
| `claude` | `~/.claude/CLAUDE.md` → `## Lessons` | every Claude Code session (mirrored to memory repo) |
| `assistant` | `prompts/observer-batch-prompt.md` → `## Lessons` | the Observer, every pulse (verdict/merge/cleanup policy) |
| `ffp` | `firefly-platform/.claude/rules/ffp-lessons.md` | FFP/Squirrel sessions (path-scoped, auto-committed) |
| `archffp` | `architect-ffp/.claude/rules/archffp-lessons.md` | archffp pipeline sessions (path-scoped, auto-committed) |
| `assistant-repo` | `.claude/rules/assistant-lessons.md` | this repo's sessions (path-scoped, auto-committed) |

Project-scoped rules belong in a project store, **not** in CLAUDE.md where every unrelated session would load them. `assistant-curator.py audit` reports any lesson whose scope (`ffp`, `squirrel`, `archffp`, …) disagrees with its store, so misrouted rules can be migrated mechanically.

### mem0 — semantic memory

`~/.assistant/mem0/` holds **108 memories** across **5 categories** — `lesson`, `working_style`, `project`, `work_history`, `decision`. The backend (`bin/tools/mem0_backend.py`) is a **3-tier** store behind one facade, picked at construction:

1. **mem0ai + AWS Bedrock** Titan embeddings — real semantic search.
2. **mem0ai + local fastembed** — semantic, no creds/network.
3. **local JSONL + lexical (IDF)** scoring — stdlib-only last resort.

mem0ai installs cleanly only under Python 3.12, so it lives in a dedicated `<repo>/.venv-mem0`; `ensure_venv()` transparently re-execs the tool into that interpreter when `import mem0` would fail. Adds are **verbatim and idempotent** (`infer=False`, a content+metadata hash gates re-seeds), so re-running a seed is a no-op.

### Obsidian vault — human-readable notes

`~/dev/obs-elitecoder/Assistant/` holds **53 notes** mirroring the same categories into subfolders — `Lessons/` (22), `Working Style/` (16), `Decisions/` (15), plus `Work Log/<YYYY-MM>/` for receipts and `Projects/`. `bin/tools/obsidian-write.py` is the single sanctioned writer; lesson confirms and work receipts mirror into the vault automatically.

### Cross-machine sync — the `mukul-memory` repo

A private GitHub repo (personal account, `github-personal` SSH host) is the canonical cross-machine store: `lessons.md`, `memories.jsonl`, the synced Obsidian folders, `config.json`, and `scripts/`. **Push-on-write** fires (detached, non-blocking) after a CLAUDE.md lesson confirm or a memory add; **pull** is manual or scheduled (`scripts/sync-pull.sh` rebuilds the local mem0 chroma index and lays down notes). A `MEMORY_SYNC_IN_PROGRESS` guard keeps a pull→import→push loop from forming. New machine: `git clone … ~/dev/mukul-memory && bash scripts/install.sh`.

## 🧰 Tool registry

The warm comms session — and any caller — invokes **named tools** instead of hand-rolling shell pipelines against `~/.assistant`. The registry lives in `bin/tools-manifest.json`; each tool is a standalone script under `bin/tools/` that emits clean JSON. `bin/tool-dispatch.py` is the single sanctioned entry point: it validates the name, type-checks/normalizes args against the manifest, execs the script, and passes stdout through verbatim (it never raises — dispatcher errors come back as `{"error": …}`).

```bash
bin/tool-dispatch.py --list                          # the manifest as JSON
bin/tool-dispatch.py fleet_status
bin/tool-dispatch.py workspace_peek --ws workspace:45
bin/tool-dispatch.py mem0_search --query "how does Mukul like status updates"
```

| Tool | What it returns |
|---|---|
| `fleet_status` | State of all workspaces — classifications, what needs attention, pulse age |
| `workspace_peek` | Live terminal screen for one workspace |
| `recent_actions` | Last N verified actions from the ledger (optionally one ws) |
| `thread_context` | Recent Telegram conversation thread |
| `system_health` | Heartbeat age, pulse index, launchd status |
| `propose_lesson` | Record a lesson proposal — or `--confirm <id>` to apply a pending one |
| `write_receipt` | Write a work receipt for a workspace before cleanup |
| `pre_cleanup_check` | Gate `/cleanup` on a receipt existing |
| `obsidian_write` / `obsidian_search` | Write / keyword-search the Obsidian vault |
| `mem0_add` / `mem0_search` | Add / semantic-search the mem0 store (category-tagged) |

## 🧾 Work receipts — a gate on `/cleanup`

Tearing down a cmux workspace destroys the only record that the work was reviewable. So before the orchestrator auto-cleans a `ready_for_cleanup` workspace, `pre-cleanup-check.py` requires a **work receipt** on disk — no receipt ⇒ `gate: block`, and the pulse emits a `needs_user` card + ping instead of cleaning up (fail-safe: a broken gate also blocks).

`bin/tools/write-receipt.py` records what shipped: project, PR number, CI status, reviewer sign-off, test count, a one-line summary, and a computed `quality_score` (high = CI green **and** approved; medium = one of them; low = CI red or abandoned). It writes two sinks — a canonical per-workspace `~/.assistant/receipts/<ws>-<ts>.json` and an append-only `~/dev/generated-docs/work-receipts.jsonl` — and mirrors a `work_history` note into the Obsidian vault. The dashboard's **Fleet DONE column** renders the receipt's quality badge + PR link + reviewer status.

## 🖥️ Single-process daemon (v2)

`src/assistant/` is an additive **single-process** path that collapses the two-LaunchAgent model (the pulse timer + the comms KeepAlive daemon) into one binary, `python -m assistant`. A `DaemonProcess` owns four subsystem threads — **pulse** (supervises `bin/pulse.py` on an interval), **comms** (outbound ledger broadcasts + heartbeat paging), **heartbeat** (snapshots every subsystem), and **tools** (the dispatch entry point) — with one shared `Config` + shutdown `Event`, a PID-file singleton, and per-thread crash isolation.

> [!IMPORTANT]
> The daemon plist (`com.mukul.assistant-daemon`) is **written but not loaded** by the installer. The migration is intentional and by-hand: the legacy `com.assistant.assistant-pulse` + `…-comms` agents keep running until you switch over (the plist header documents the exact `launchctl` dance). The daemon's CommsSubsystem is **outbound-only** — it does not run the inbound warm-session reply loop, and two long-pollers on one bot token collide (Telegram 409).

## 📂 Repo layout

```
assistant/
├── bin/                      # daemons, helpers, the per-pulse pipeline (36 scripts)
│   ├── tools/                # named-tool registry scripts (16) + tools-manifest.json
│   └── ...
├── src/assistant/            # opt-in single-process daemon (pulse + comms + heartbeat + tools)
├── prompts/
│   ├── observer-batch-prompt.md   # the only LLM prompt in the loop (+ ## Lessons)
│   └── prompt-assistant-comms-warm.md
├── skills/                   # /todo, /cleanup, /spawn-claude-workspace, /back-off, /attend, /lesson
├── evals/observer/           # 13 real-transcript fixtures × Sonnet → verdict assertions
├── tests/                    # Python unit tests (29 files, no LLM)
├── slack-reactor/            # companion: Slack emoji react → /todo
├── launchagents/             # macOS LaunchAgent plists (8 loaded + daemon/watcher opt-in)
├── hooks/                    # vendored cmux session-restore hooks
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
| `bin/` (incl. `bin/tools/`) | `~/.claude/bin` | symlink (edits go live, no copy) |
| `docs/assistant-operating-guide.md` | `~/.claude/assistant-operating-guide.md` | symlink |
| `skills/<name>/` | `~/.claude/skills/<name>/` | **symlink** (repo is the single source of truth) |
| `hooks/*.py` + `bin/cmux-restore-sessions.py` | `~/.claude/hooks/`, `~/.local/bin/` | symlink (cmux session-restore) |
| `launchagents/*.plist` | `~/Library/LaunchAgents/*.plist` | copy + launchctl reload (only the 8 it manages) |

The installer **symlinks** skills (a `git pull` is live immediately, and a self-update can never clobber a live edit by re-copying), reads `prompts/observer-batch-prompt.md` directly from the repo, decommissions legacy plists/lesson-stores, and **skips** the opt-in daemon + cmux-watcher plists (it writes the watcher plist but never loads it). `./install.sh --pull-skills` recovers edits made before a skill was symlinked. The cross-machine memory repo is installed separately: `bash ~/dev/mukul-memory/scripts/install.sh`.

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
python3 -m unittest discover tests -v        # 29 files
```

| File | Pins |
|---|---|
| `test_pulse.py` | inbox drain, JSONL parse, verdict→action lookup, NO_INGEST_GUARD, chunker |
| `test_self_update.py` | ff-only pull, refusal on dirty/ahead tree, install-needed classification |
| `test_daemon.py` | daemon lifecycle, subsystem supervision + crash isolation, PID singleton |
| `test_back_off.py` / `…_in_process.py` | back-off list filter is applied; CLI add/remove/list |
| `test_purge_stale_awaiting.py` | drop predicates: closed workspaces, done TODOs, cmux-down safety |
| `test_build_ws_context.py` / `…_in_process.py` | mechanical signals; no PR data leaks into output |
| `test_pickers_in_process.py` | `pick-ws-batch`, `pick-open-todos` selection logic |
| `test_save_summary_in_process.py` | verdict persistence; rejects verdicts missing `next` |
| `test_send_dispatch_in_process.py` | `cmux-send`, `merge-pr-dispatch`, `cmux-ws-numberer` — never call cmux/gh for real |
| `test_writers_in_process.py` | `state-write`, actions-ledger, transcript-tail, curator |
| `test_renderer_in_process.py` | dashboard HTML renderer (incl. Fleet tab) |
| `test_tool_dispatch.py` | manifest load/validation, arg coercion + choices, stdout pass-through |
| `test_work_receipts.py` | receipt write, quality-score mapping, pre-cleanup gate (block on no receipt) |
| `test_lesson_extractor.py` | ledger + transcript pattern detection, dedup, draft, proposal write |
| `test_lesson_routing.py` | curator's 5 stores, project-store auto-commit, misrouted audit |
| `test_cmux_watcher.py` | event classification, pattern-bank hot-reload + mute, de-dup/cooldown, inbox drop |
| `test_pattern_learning.py` | noise feedback → pattern downgrade; `--discover` candidate proposal |
| `test_mem0_tools.py` / `test_mem0_real.py` | mem0 backend tiers (local + real), idempotent add, category filter |
| `test_obsidian_tools.py` | vault write (category→subfolder, never-overwrite) + keyword/frontmatter search |
| `test_comms_lib.py` / `test_tg_poll.py` / `test_tg_send.py` | comms helpers + Telegram poll/send |
| `test_comms_session.py` | warm session spawn/feed/read, clear-and-resume threshold |
| `test_conversation.py` / `test_threading_tools.py` | comms durable chat memory; message↔ledger threading |
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

# Add a tool: drop a script in bin/tools/ + a manifest entry; it's callable immediately.
$EDITOR bin/tools-manifest.json
bin/tool-dispatch.py --list

# Edit a skill or plist — re-run install.
$EDITOR skills/cleanup/cleanup.sh
./install.sh --apply

# Trigger a pulse manually:
launchctl kickstart -k gui/$UID/com.assistant.assistant-pulse

# Pause / resume the orchestrator:
launchctl bootout  gui/$UID/com.assistant.assistant-pulse
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist
```

## 💾 Runtime state — NOT committed

Regenerated continuously under `~/.claude/cache/`, `~/.assistant/`, and `~/dev/{mukul-memory,obs-elitecoder,generated-docs}/`:

- `~/.claude/cache/assistant-state.json` — orchestrator decisions (most recent pulse)
- `~/.claude/assistant-todo.json` — TODO data
- `~/.claude/cache/world.json` — workspace inventory (emitted by `world-scanner`)
- `~/.assistant/heartbeat.json` — pulse health, drives dashboard banner
- `~/.assistant/back-off.json` — workspaces the orchestrator must skip
- `~/.assistant/sends.jsonl` — every cmux send (literal text + byte delta)
- `~/.assistant/actions-ledger.jsonl` — every action with `verified_via` proof field
- `~/.assistant/observer-summaries/workspace_*.json` — most recent verdict per ws (powers dashboard Workspaces tab)
- `~/.assistant/observer-runs/<pulse_idx>/batch-<batch_idx>/` — full Observer call audit (kept forever)
- `~/.assistant/inbox/*.json` — event-driven signals (cmux-watcher drops, slack-reactor) the pulse/comms drains
- `~/.assistant/pattern_bank.json` + `cmux-fired-patterns.jsonl` — cmux-watcher patterns + fired-pattern audit
- `~/.assistant/comms/proposals.jsonl` — pending/confirmed lesson + pattern proposals
- `~/.assistant/receipts/*.json` — work receipts the pre-cleanup gate + Fleet DONE column read
- `~/.assistant/mem0/` — mem0 store (`memories.jsonl` + `chroma/` index); `.venv-mem0/` is the Py3.12 venv
- `~/.assistant/self-update.json` — self-update throttle marker
- `~/.architect/orchestrator-ledger/cleanup-*.json` — `/cleanup --undo` history
- `~/.assistant/comms/config.json` — comms bot token + chat_ids (chmod 600; **never committed**)
- `~/.assistant/comms/conversation.jsonl` — comms durable chat memory (rebuilt into context each turn)
- `~/.assistant/comms/session.json` — warm cmux session registry (ws_ref / surface_ref / transcript)
- `~/.assistant/comms/{threads.jsonl,ledger.cursor,tg.cursor,heartbeat.json,comms-listen.pid}` — threading + cursors + own heartbeat + singleton lock

## 📓 Lessons

Lessons are routed to one of **five** stores by `--target` (see [Memory & learning](#-memory--learning)). The default — personal coding/workflow rules — lands in `~/.claude/CLAUDE.md` under `## Lessons`, which Claude Code auto-loads into every session.

```bash
~/.claude/bin/assistant-curator.py write \
  --trigger "<one-line situation>" \
  --rule "<what to do or not do>" \
  --target "claude|assistant|ffp|archffp|assistant-repo" \
  --scope "<sub-domain for the target>"
```

Subcommands: `list`, `rm <slug>`, `audit` (find misrouted project-scoped lessons), `trim` (open a store in `$EDITOR`). Project targets auto-commit the rules file to their repo; CLAUDE.md lessons sync to the cross-machine memory repo.

## 🎯 Design notes

- **Mechanical orchestrator, isolated LLM.** The Observer prompt is the only LLM in the loop and its only job is judgment. Verdict→action is a Python dict, not a prompt example the LLM can drift from. Bugs are diffs + unit tests, not prompt rewrites + restart cycles.
- **PR data is fetched by branch, not prose.** `gh pr view --head <branch>` from cwd. No regex-scraping PR numbers out of the agent's narrative — that pipeline (deleted 2026-05-26) caused the production bug where unrelated merged PRs drove workspace-close decisions.
- **Observer reads transcripts directly.** No curated turns, no truncation. If it needs the original prompt, it `head -30`s; if it needs to know what tools ran, it scrolls back.
- **A wrong transcript is worse than none.** `build-ws-context.py` attaches `transcript_path` only when verified to belong to the workspace's live agent — never guessed by mtime/cwd/title. No verified signal ⇒ `transcript_path: null`, and the Observer judges from `screen_text` (the agent pane's live terminal, which it trusts over transcript-derived signals on conflict).
- **The orchestrator and `/cleanup` both refuse to close workspaces.** That's the user's call. Auto-close is gone. `/cleanup` is additionally gated on a work receipt.
- **Single sanctioned paths.** Every keystroke goes through `bin/cmux-send.py`; every named tool through `bin/tool-dispatch.py`; every lesson through `assistant-curator.py`. Without that, "I did it" is unverifiable and behavior drifts.
- **NO_INGEST_GUARD.** If the last send to a ws returned `transcript_size_delta=0` (cmux returned OK but no claude PID was reading), the orchestrator skips the resend on the next pulse. Breaks the cleanup-loop class of bug structurally.
- **Project rules live in project stores.** A project-scoped lesson in CLAUDE.md would load for every unrelated session; the curator routes it to the repo's `.claude/rules/` and `audit` flags any that are misrouted.
- **Self-update refuses to discard work.** `self_update.py` does `git pull --ff-only` only on a clean, non-ahead tree — never `reset`/`clean`/`force`. A dirty or diverged tree is surfaced, not steamrolled.
- **The single-process daemon and cmux-watcher are opt-in.** Their plists are committed and written, but the installer never loads them — activation is an explicit, by-hand `launchctl load`, per the "ask before loading a persistent service" rule.
- **Back-off list.** `~/.assistant/back-off.json` excludes workspaces from every per-ws step. Manageable from inside any cmux workspace via the `/back-off` and `/attend` skills.

<div align="center">
<sub>Mukul's personal fleet manager for parallel cmux Claude Code workspaces · no LLM in orchestration · memory + learning + event-driven delivery · everything auditable</sub>
</div>
</content>
</invoke>
