# workspace-watcher — cmux unexpected-close detector + auto-resumer

**Why this exists.** cmux workspaces holding active claude sessions silently die
(zsh `killjb→waitjobs` SIGSEGV on macOS 26.x, node-bundle bridge crashes, OS
reboots, kill -9). Without this daemon, the parent manager session has no way
to find out and Mukul only notices hours later when scanning the sidebar. See
`~/dev/generated-docs/cmux-crash-detection-investigation.md` for the death-rate
data and the design that motivated this code.

## What it watches

- **Subscription:** `cmux events --category workspace --reconnect --no-heartbeat --no-ack --cursor-file ~/.assistant/workspace-watcher/cursor.seq`
- **State sources for the close payload:**
  - `cmux rpc workspace.list '{}'` — UUID/ref/title/cwd/selected
  - `~/Library/Application Support/cmux/session-com.cmuxterm.app.json` — `resumeBinding.command`, `checkpointId`, `wasAgentRunning`, browser `urlString` (PR link)
  - `~/.architect/orchestrator-ledger/cleanup-*.json` — recorded as evidence on the file drop, but **not** used for classification
  - `~/Library/Logs/DiagnosticReports/*.ips` — the sole positive crash signal

## What it does on `workspace.closed`

1. **Refresh** the in-memory workspace registry (so the close UUID can still be resolved).
2. **Classify cause** — only two outcomes:
   - `crash` — a `com.cmuxterm.app`-coalition `.ips` (spindump) with `captureTime` in the asymmetric window `[close − 60s, close + 10s]`. Cause precedes effect: the spindump's `captureTime` is when the child died, the workspace event lands shortly after; macOS sometimes delays the .ips write by several seconds.
   - `intentional` — default. The cmux daemon delivered `workspace.closed`, which means cmux is alive and processed the close — the user (or a tool acting on their behalf — UI X button, `cmux close-workspace`, the `/close-workspace` skill, `/cleanup`) asked for it. A cleanup-ledger match is recorded in `evidence.matching_cleanup_ledger` but does **not** influence classification.
3. **File drop** at `~/.claude/cmux-crash-events/<workspace-N>-<epoch>.json` for every close, including intentional ones (manager session reads this on its next active turn — see `Manager-side polling`).
4. **Notification + auto-resume** — fire **only** on `cause=crash`. Default-intentional means an ordinary close never bounces back as a phantom workspace. Sound: Sosumi for crash, Glass on successful resume, Funk when resume is blocked or fails.

## Resume policy

- Resume fires only on `cause=crash`. `was_agent_running` is metadata in the file drop now, not a vote.
- **Per-workspace cap:** 2 resumes per rolling hour (avoid an infinite zsh-segfault loop).
- **Daemon-wide cap:** 5 resumes per rolling hour (system-wide blast-radius cap).
- **Pre-conditions for resume:** `cause=crash`, `resume_command` present in registry cache, governor under cap.
- **Three-strike retry across events:** the daemon does one resume per close event. If the resumed workspace dies again within ~30 s, that's a fresh `workspace.closed` whose own per-ws cap budget governs whether a second resume fires; the planned `bash -lc '…'` fallback for the `.zprofile` segfault path is not yet wired (see "Future work").

When a resume is blocked or fails, a "needs you" notification fires with the reason (sound: Funk).

## Known false-negative

A workspace that dies **without** writing a com.cmuxterm.app-coalition `.ips` is classified as intentional and will not auto-resume. Examples: a `SIGKILL` of a child (no spindump), an OOM-kill, cmux internal state corruption that closes the workspace without a child crash. Probe upstream signals with `cmux events --category '*' --reconnect | jq -r .name | sort -u` while exercising controlled deaths if you want to widen detection coverage.

## File-drop schema (v2)

```json
{
  "schema_version": 2,
  "workspace_ref": "workspace:26",
  "workspace_id": "C0EB...UUID",
  "name": "P2 archffp connections-D-key",
  "cwd": "/Users/mukuls/dev/firefly-platform/.claude/worktrees/...",
  "died_at": "2026-05-28T15:08:06Z",
  "cause": "crash",
  "evidence": {
    "matching_ips": "/Users/mukuls/Library/Logs/DiagnosticReports/zsh-2026-05-28-080806.ips",
    "matching_cleanup_ledger": null,
    "ips_window": {
      "lookback_sec": 60,
      "lookahead_sec": 10,
      "close_epoch": 1779990063.0
    },
    "ips_summary": {
      "path": "...",
      "proc": "zsh",
      "responsible": "cmux",
      "coalition": "com.cmuxterm.app",
      "captureTime": "2026-05-28 08:08:06.7897 -0700",
      "signal": "SIGSEGV",
      "top": "killjb"
    }
  },
  "last_session_uuid": "abe0b25e-6eda-4187-86e1-1dab4b704faf",
  "last_pr": "https://github.com/Adobe-Firefly/firefly-platform/pull/10646",
  "was_agent_running": true,
  "process_title_at_close": "✳ Resume connections P2 after crash",
  "resume": {
    "attempted": true,
    "ok": true,
    "new_ref": "workspace:48",
    "new_uuid": "...",
    "attempt_no": 1,
    "used_bash_lc": false
  }
}
```

## Manager-side polling

The manager session (a sibling claude) reads `~/.claude/cmux-crash-events/*.json`
on its next active turn — entries are append-only, never mutated. The manager
should:

1. List the directory by mtime descending.
2. Treat each unread entry as a notification it needs to act on (typically: log
   what happened to its own state.md, surface to Mukul, and decide whether the
   resumed child workspace needs additional setup).
3. Mark entries "read" by moving them to `~/.claude/cmux-crash-events/read/`
   (the daemon never deletes — manager owns archival).

## How to install / load

The daemon is **not** loaded by default — Mukul has to explicitly run:

```bash
# Install the LaunchAgent symlink
ln -sf /Users/mukuls/dev/assistant/launchagents/com.assistant.workspace-watcher.plist \
       ~/Library/LaunchAgents/com.assistant.workspace-watcher.plist

# Load it (KeepAlive=true, so it stays alive after restarts)
launchctl load -w ~/Library/LaunchAgents/com.assistant.workspace-watcher.plist
```

## How to disable

```bash
launchctl unload -w ~/Library/LaunchAgents/com.assistant.workspace-watcher.plist
```

To pause without unloading (e.g. while debugging), `launchctl stop com.assistant.workspace-watcher` once is enough — KeepAlive will respawn it on next event, but if you uncheck `RunAtLoad` first, you can suppress restart.

## How to inspect

```bash
# Daemon log
tail -f ~/.claude/logs/workspace-watcher.log

# Resume-attempt ledger (one JSON line per attempt)
tail -f ~/.assistant/workspace-watcher/resume-ledger.jsonl

# Live workspace cache the daemon uses to resolve close UUIDs
jq . ~/.assistant/workspace-watcher/ws-cache.json | head -60

# Crash-event file drops (manager polls this)
ls -lt ~/.claude/cmux-crash-events/

# launchd stdout/stderr
tail ~/.assistant/logs/workspace-watcher.launchd.{out,err}
```

## How to test

Run the daemon in the foreground while triggering events. Default-intentional means
no fake cleanup-ledger entry is needed for the intentional-close smoke.

```bash
# Terminal A
pkill -9 -f workspace-watcher.py 2>/dev/null   # avoid double-instances
python3 ~/dev/assistant/bin/workspace-watcher.py

# Terminal B — intentional close (no .ips → cause=intentional)
cmux new-workspace --name "smoke-intentional" --cwd /tmp --command "sleep 600" --focus false
WS_UUID=$(cmux rpc workspace.list '{}' | python3 -c "
import json,sys
for w in json.load(sys.stdin).get('workspaces', []):
    if 'smoke-intentional' in (w.get('title') or ''):
        print(w['id']); break
")
cmux rpc workspace.close "{\"workspace_id\":\"$WS_UUID\"}"
# Expect: file drop with cause=intentional, NO notification, NO resume.

# Terminal C — synthetic crash (kill -SEGV $$ writes a com.cmuxterm.app .ips)
cmux new-workspace --name "smoke-crash" --cwd /tmp --command 'sleep 1; kill -SEGV $$' --focus false
# Expect: cause=crash, evidence.matching_ips populated, Sosumi notification.
# resume.attempted=false, reason="no resume_command" (bare zsh has no agent).
```

## Failure modes (known)

- **cmux itself dies → no event stream → no signal.** The watcher's launchd `KeepAlive`
  will respawn the watcher when cmux comes back, but during the cmux outage we're blind.
  Detection of this state is out of scope for v1; consider a freshness check on the
  cursor file in v2.
- **Move-between-windows race:** dragging a workspace from one window to another
  emits `workspace.closed` then `workspace.created` within milliseconds. v1 does not
  yet suppress this — if you see a spurious "died" notification immediately after a
  move, that's the cause. Fix: deduplicate by title within 2 s of close. Tracked in
  `Future work`.
- **Auto-resume into the same crash:** if a `.zprofile` recursion is hard-failing,
  the resumed workspace will die identically. The per-workspace cap (2/h) limits
  the loop to 2 attempts; the second attempt will use `bash -lc '…'` once that's
  shipped.

## Future work

- Suppress move-between-windows false positives (2 s look-ahead for matching `created`).
- Second-strike `bash -lc '…'` fallback for the `.zprofile` segfault path.
- Daemon-self-health probe: if the cursor file hasn't advanced in 5 min and `cmux ping` works, the watcher is wedged → log + restart.
- Slack channel — deferred per global "never send Slack" rule until Mukul explicitly opts in for this signal.
