<div align="center">

# 🛰️ Assistant

**Your AI chief of staff — watches a fleet of Claude Code workspaces, acts on what's safe, and gets smarter the longer you use it.**

</div>

---

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/elitecoder/assistant/main/install-bootstrap.sh)
```

Prerequisites: `git`, `python3`, and the [`claude` CLI](https://claude.ai/code). The script clones the repo to `~/dev/assistant`, wires up symlinks and LaunchAgent plists. One manual step at the end: `launchctl load` the plists.

## What it is

Spin up a dozen Claude Code agents in parallel and walk away. Assistant watches them while you're gone — merging PRs, cleaning up finished work, nudging stalled agents — and surfaces what needs your attention on its dashboard.

The longer you use it, the smarter it gets. It reads your own session history to find patterns, turns corrections and confirmations into rules, and asks if you want to keep them. Confirmed rules sync across all your machines automatically.

## What it can do

**📥 Capture work from a Slack emoji.** React to any Slack thread with this machine's emoji (`TODO_EMOJI`, default `mukuls2`) and the whole thread — every message, plus a link back — is captured as a TODO in `~/.claude/assistant-todo.json`, the same store the `/todo` skill, the pulse, and the dashboard read. Per-machine emoji routing means a shared bot can fan reactions out to whichever laptop owns that emoji. See [`slack-reactor/`](slack-reactor/README.md).

**🚀 Dispatch TODOs while keeping the fleet from overloading.** A TODO flagged `autoDispatch=true` that's never been spawned gets picked up by the next pulse and dropped into a *fresh* cmux workspace — prompt staged on disk, delivered to the surface, confirmed by watching the transcript grow. Load is bounded by hard caps: at most `ACTIVE_WS_CAP=5` busy workspaces, `TOTAL_WS_CAP=30` total, and `MAX_DISPATCH_PER_PULSE=2` new spawns per cycle. Hit a cap and dispatch waits — the fleet never runs away from you.

**🧠 Turn corrections into rules, then into memory.** `lesson-extractor.py` scans your recent Claude Code transcripts and the action ledger for corrections, confirmations, and recurring questions, and distills lesson candidates into a proposals queue for you to review. Confirm one and it's routed to the right store (one of `claude` / `assistant` / `ffp` / `archffp` / `assistant-repo`), then mirrored into the Obsidian vault and synced to the cross-machine memory repo that feeds Mem0 semantic memory. Nothing is added without your confirmation.

**👋 Nudge stalled work and move the safe stuff forward.** Each pulse the Observer emits one verdict per workspace, and Python — not the model — turns it into an action. `ready_for_merge` queues `/merge-when-ready`; `ready_for_cleanup` sends `/cleanup` (only on workspaces *it* queued the merge for, and only once a work receipt exists); `stranded` nudges the idle agent with what failed and a retry; `needs_user` surfaces an awaiting card and does nothing else. Autonomy is fenced by a back-off list, the work-receipt gate, the assistant-merge ledger, and `NO_INGEST_GUARD`.

## Entry points

- `bin/pulse.py` — the main orchestrator loop, runs every 5 min via LaunchAgent
- `bin/cmux-watcher.py` — event-driven workspace signal delivery (opt-in LaunchAgent)
- `bin/assistant-curator.py` — lesson/rule management across all five stores
- `bin/tool-dispatch.py` — named tool dispatcher (`bin/tools-manifest.json`)
- `install.sh --apply` — wires everything up; symlinks skills; writes plists (never loads them)

## Architecture decisions (the why, not the what)

**Only one LLM call per pulse.** The Observer's only job is to emit a verdict. Turning a verdict into an action is a Python dictionary. This means bugs are diffs and unit tests, not prompt rewrites — and behavior can't drift because the model decided to do something different.

**PR data comes from `gh`, not from reading the agent's prose.** An earlier version scraped PR numbers from transcript text and auto-closed workspaces based on unrelated merged PRs mentioned in passing. That pipeline is gone. `gh pr view --head <branch>` from cwd only.

**A wrong transcript is worse than none.** `build-ws-context.py` attaches `transcript_path` only when verified to belong to the workspace's live agent. No verified signal → `transcript_path: null`. The Observer judges from the live terminal screen rather than guess from a mismatched transcript.

**Project-scoped lessons belong in project repos, not CLAUDE.md.** A lesson about FFP/Squirrel loading into every unrelated coding session is noise. `assistant-curator.py` routes by `--target` (claude / assistant / ffp / archffp / assistant-repo) and auto-commits project stores to their repo's `.claude/rules/`.

**Memory is layered.** Lessons (rules → CLAUDE.md or project stores), semantic memory (Mem0 at `~/.assistant/mem0/`), human-readable notes (Obsidian vault at `~/dev/obs-elitecoder/Assistant/`), and cross-machine sync (private `mukul-memory` repo). Each layer has a different audience: the LLM, the agent doing semantic search, the human browsing notes.

**Patterns learn from feedback.** The cmux-watcher pattern bank (`~/.assistant/pattern_bank.json`) hot-reloads on file change. Patterns that generate noise from user corrections get downgraded to `muted` automatically via `bin/tools/pattern-feedback.py`.

## Absolute constraints

These are structural, not just conventions — violating them will cause real problems:

- **Never close a cmux workspace.** The orchestrator sends slash commands into workspaces; it never reaches in and closes them. That's the user's call. `/cleanup` is additionally gated on a work receipt existing.
- **Never `launchctl load` automatically.** `install.sh` writes plists but never loads them. The cmux-watcher and single-process daemon plists are opt-in and always loaded by hand.
- **Never widen your own permissions.** Self-improvement edits stay within `~/dev/assistant`, never `~/.claude` global rules. `/update-config` from an agent is blocked.
- **Never trust the header port for archffp teardown.** When two archffp worktrees run concurrently, vite falls back to PORT+1 but the header still shows the original port. Always reconcile against the real listening PID before killing anything.

## Gotchas

- **Self-update refuses a dirty or ahead tree.** `self_update.py` does `git pull --ff-only` only. A dirty tree is surfaced, never steamrolled.
- **NO_INGEST_GUARD:** if the last send to a workspace returned `transcript_size_delta=0` (cmux sent OK but no Claude process was reading), the orchestrator skips the next resend. This breaks the cleanup-resend-loop class of bug structurally.
- **The single-process daemon (`src/assistant/`) is opt-in.** The legacy pulse LaunchAgent keeps running until you explicitly switch over.
- **mem0ai requires Python 3.12.** It lives in `.venv-mem0`; `ensure_venv()` transparently re-execs tools into that interpreter.

## Testing

```bash
python3 -m unittest discover tests -v    # 35 test files, no LLM
cd evals/observer && ./run.py            # 13 real-transcript fixtures × Observer
```

The headline eval fixture (`01-ws97-trap-no-pr-mid-audit`) replays the production bug where an unrelated merged PR in transcript prose drove an auto-close. Run the evals after any change to `prompts/observer-batch-prompt.md` or `bin/build-ws-context.py`.

<div align="center">
<sub>Personal fleet manager for parallel cmux Claude Code workspaces · macOS</sub>
</div>
