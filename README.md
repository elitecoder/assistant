<div align="center">

# 🛰️ Assistant

**Your AI chief of staff — watches a fleet of Claude Code workspaces, keeps you in the loop, and gets smarter the longer you use it.**

</div>

---

## What it is

Spin up a dozen Claude Code agents in parallel and walk away. Assistant watches them while you're gone — merging PRs, cleaning up finished work, nudging stalled agents — and texts you the moment something needs your attention. When you reply from your phone, a warm Claude session answers in seconds with a full picture of what's going on.

The longer you use it, the smarter it gets. It reads your own session history to find patterns, turns corrections and confirmations into rules, and asks if you want to keep them. Confirmed rules sync across all your machines automatically.

**Slack integration:** React to any Slack thread with this machine's emoji and the whole thread is captured as a TODO, which the next pulse auto-dispatches into a fresh workspace. See [`slack-reactor/`](slack-reactor/README.md).

## Entry points

- `bin/pulse.py` — the main orchestrator loop, runs every 5 min via LaunchAgent
- `bin/comms-listen.py` — the Telegram daemon (inbound + outbound pings + heartbeat page)
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
- **Never widen your own permissions.** The warm comms session can edit `~/dev/assistant` (self-improvement) but not `~/.claude` global rules. `/update-config` from an agent is blocked.
- **Never send Telegram messages without confirmation.** All Telegram sends go through `bin/tg-send.py`. Mutations (lesson add, restart, respawn) require a human `y` on a separate message.
- **Never trust the header port for archffp teardown.** When two archffp worktrees run concurrently, vite falls back to PORT+1 but the header still shows the original port. Always reconcile against the real listening PID before killing anything.

## Gotchas

- **Self-update refuses a dirty or ahead tree.** `self_update.py` does `git pull --ff-only` only. A dirty tree is surfaced, never steamrolled.
- **NO_INGEST_GUARD:** if the last send to a workspace returned `transcript_size_delta=0` (cmux sent OK but no Claude process was reading), the orchestrator skips the next resend. This breaks the cleanup-resend-loop class of bug structurally.
- **The single-process daemon (`src/assistant/`) is opt-in.** The legacy two-LaunchAgent model (pulse + comms) keeps running until you explicitly switch over.
- **mem0ai requires Python 3.12.** It lives in `.venv-mem0`; `ensure_venv()` transparently re-execs tools into that interpreter.
- **The inbox is kqueue-watched, not polled.** `comms-listen.py` uses `select.kqueue()` for instant delivery when `cmux-watcher.py` drops an event. The poll fallback is Linux only.

## Testing

```bash
python3 -m unittest discover tests -v    # 29 test files, no LLM
cd evals/observer && ./run.py            # 13 real-transcript fixtures × Observer
```

The headline eval fixture (`01-ws97-trap-no-pr-mid-audit`) replays the production bug where an unrelated merged PR in transcript prose drove an auto-close. Run the evals after any change to `prompts/observer-batch-prompt.md` or `bin/build-ws-context.py`.

<div align="center">
<sub>Personal fleet manager for parallel cmux Claude Code workspaces · macOS</sub>
</div>
