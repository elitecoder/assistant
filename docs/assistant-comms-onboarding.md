# Assistant-comms — Onboarding

**assistant-comms** watches **Assistant** and talks to you over Telegram. It pings you when Assistant takes a verified action, pages you when Assistant's heartbeat goes stale, and lets you converse — ask "how's it going?", get a real answer — plus a recovery surface (`lesson` / `restart` / `respawn`) when Assistant is wrong.

It runs **headless and one-shot**: a LaunchAgent fires `bin/comms-pulse.py` every 300s, which invokes `claude --print` once, runs a single pulse, and exits. No persistent session, no Terminal window, no tty, no AppleScript. Each pulse is a brand-new process — its memory lives entirely on disk (`conversation.jsonl` + cursors + threads), so a crash, `/clear`, or auto-compact loses nothing.

## Why headless

The earlier design ran comms as a visible Terminal.app session woken by the pulse. That meant tracking a tty, replaying keystrokes into an initializing shell, and grepping the window for a banner — all flaky AppleScript timing, all deleted. Because comms already keeps its state on disk (it was built that way for restart-resilience), a disposable per-pulse `claude --print` is strictly simpler and removes the entire osascript failure surface. You supervise via Telegram itself, `conversation.jsonl`, and the launchd logs — the phone is the window.

## What you'll see

| Trigger | Telegram message |
|---|---|
| Assistant appends a verified action to its ledger | `[cleanup] ok assistant:close-clean:workspace:117 ws=workspace:117 td=- pulse=38 via=jsonl_transcript` |
| Same, but evidence is `screen_read` (Assistant rejects this as weak) | `(!)screen_read` flag in `via=` |
| Assistant heartbeat stale (>10 min) or status flips to `frozen`/`stale_world`/`respawn-requested` | `Assistant heartbeat stale ws=workspace:18 status=frozen last pulse 12m ago` |

Heartbeat alerts dedupe at 30 min. Action pings are one-per-line, formatted by `comms_lib.fmt_action_line`.

## What you can text back

It's a conversation, not a verb menu. Write a full sentence ("why did it close ws:117?", "is Assistant healthy?") and comms reconstructs the recent thread from `conversation.jsonl`, reasons over Assistant's real state, and replies. If you reply to a specific ping, comms resolves what that ping was about and answers in context.

These shorthands are recognized as fast-paths but aren't required:

- `ping` — pong + uptime + last action age
- `last [N]` — tail N entries from the actions ledger (default 5, max 20)
- `screen` — last 50 lines of Assistant's cmux screen
- `state` — heartbeat snapshot + observer-report summary
- `mute Nm` / `mute Nh` — suppress non-urgent pings; `unmute` clears it

**Mutating Assistant — comms proposes, you reply `y` on the next pulse to confirm:**
- `lesson "<trigger>" "<rule>" [scope]` — append a lesson rule via `assistant-curator.py`
- `restart` — back-date Assistant's heartbeat so its pulse respawns it
- `respawn` — run `spawn-assistant.sh` immediately (fresh Assistant cmux workspace)

The propose and the execute happen on **different pulses** — a crash between them drops the pending action (it lived only as an unanswered proposal in `conversation.jsonl`), so a restart never auto-fires. Mute never silences stale-heartbeat alerts.

## How latency works

Comms runs on a 300s pulse. So:

- **You text the bot → reply:** 0–300s, avg ~2.5 min. The next pulse drains your message and replies.
- **Assistant takes a verified action → ping:** 0–300s. The next pulse reads the new ledger line.

This is a recovery-and-reporting surface, not a real-time chat. A headless `claude --print` cold-starts each pulse (~100–160s of model work), so 300s gives comfortable headroom; a tighter interval would just make pulses overrun and get skipped by the lock. Durable memory fixes continuity, not latency.

## Files

| Purpose | Path |
|---|---|
| Boot prompt (the agent reads this each pulse) | `~/dev/assistant/prompts/prompt-assistant-comms-agent.md` |
| Pulse wrapper (headless `claude --print` per tick) | `~/dev/assistant/bin/comms-pulse.py` |
| Helper lib (Paths, Config, formatting, cursors, threads, conversation) | `~/dev/assistant/bin/comms_lib.py` |
| CLI tools | `~/dev/assistant/bin/{tg-send,tg-poll,link-msg,lookup-thread,conversation}.py` |
| First-run setup | `~/dev/assistant/bin/assistant-comms-setup.sh` |
| Tests | `~/dev/assistant/tests/test_{comms_lib,tg_send,tg_poll,threading_tools,conversation}.py` |
| LaunchAgent | `~/Library/LaunchAgents/com.assistant.assistant-comms.plist` (repo: `launchagents/`) |
| Venv (setup + tests only; the pulse uses system `python3` + `claude`) | `~/.assistant/comms-venv/` |
| Config (token + chat_ids) | `~/.assistant/comms/config.json` (chmod 600) |
| Durable chat memory | `~/.assistant/comms/conversation.jsonl` |
| Ledger / Telegram cursors | `~/.assistant/comms/{ledger.cursor,tg.cursor}` |
| Threads (sent_msg → ledger_key) | `~/.assistant/comms/threads.jsonl` |
| Comms heartbeat (pulse writes) | `~/.assistant/comms/heartbeat.json` |
| Per-pulse run artifacts (prompt/stdout/stderr/meta) | `~/.assistant/comms/pulse-runs/pulse-<idx>-<ts>/` |
| Pulse wrapper log | `~/.assistant/comms/comms-pulse.log` |
| LaunchAgent stdout/err | `~/.architect/orchestrator-logs/assistant-comms.launchd.{out,err}` |

## Setup — one time

### 1. Prerequisites

- macOS with Python 3.13 (the setup venv needs it; the pulse uses system `python3` + the `claude` CLI).
- `uv` installed (`brew install uv`); falls back to `python3 -m venv` if absent.
- A Telegram account.
- Assistant already running (comms tails its `~/.assistant/actions-ledger.jsonl` + `~/.assistant/heartbeat.json`).
- `claude` CLI on PATH with Bedrock auth working (comms reuses the `CLAUDE_CODE_USE_BEDROCK` / `AWS_*` vars from `~/.zprofile`, same as `bin/pulse.py`).
- `~/dev/assistant` repo cloned with the comms files committed.

### 2. Make a Telegram bot

1. On Telegram, find **@BotFather**.
2. Send `/newbot`, follow prompts (name + handle ending in `bot`).
3. BotFather returns a token like `1234567890:AAAA-...`. Keep it private.

### 3. Run the setup script

```bash
~/dev/assistant/bin/assistant-comms-setup.sh
```

It will:
1. Create `~/.assistant/comms-venv/`.
2. Install `python-telegram-bot`, `pytest`, `coverage` (for the test suite; the pulse's Telegram calls use stdlib `urllib` via `tg-send.py` / `tg-poll.py`).
3. Prompt for your bot token.
4. Wait up to 120s for you to send any message to your bot — that captures your `chat_id`.
5. Write `~/.assistant/comms/config.json` (chmod 600).

If chat_id capture fails, send `/start` to your bot first, then re-run.

### 4. Smoke test by hand

Run one pulse directly — no LaunchAgent yet:

```bash
~/dev/assistant/bin/comms-pulse.py
```

It will invoke `claude --print` once (this takes ~2 min — headless cold start), drain the ledger, poll Telegram, and write its heartbeat. Inspect what it did:

```bash
cat ~/.assistant/comms/heartbeat.json          # expect status=active, a pulse_idx
ls -t ~/.assistant/comms/pulse-runs | head -1  # newest run dir
cat ~/.assistant/comms/pulse-runs/<newest>/stdout.txt   # the pulse-trace line
```

Then send your bot a message (`how's Assistant doing?`) and run the pulse again — you should get a reply on your phone within the pulse. A `--dry-run` flag skips the `claude` call if you just want to test wiring.

If the pulse exits non-zero, check `~/.assistant/comms/comms-pulse.log` and the run dir's `stderr.txt`.

### 5. Install + load the LaunchAgent

The repo's `install.sh` copies changed plists and reloads only those:

```bash
cd ~/dev/assistant
./install.sh            # dry-run — shows what would change
./install.sh --apply    # copies the comms plist + loads it (leaves unchanged agents alone)
```

It diffs each plist against the installed copy and only reloads the ones that differ, so this won't disturb the running Assistant or the other daemons.

Verify:
```bash
launchctl list | grep assistant-comms
# Expect: PID  0  com.assistant.assistant-comms
```

To stop / start manually:
```bash
launchctl bootout  gui/$UID/com.assistant.assistant-comms
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.assistant-comms.plist

# Fire one pulse on demand:
launchctl kickstart -k gui/$UID/com.assistant.assistant-comms
```

## How the loop works

```
every 300s:  com.assistant.assistant-comms  →  bin/comms-pulse.py
   1. acquire lock (skip if a prior pulse is still running)
   2. parse Bedrock env from ~/.zprofile, merge onto subprocess env
   3. claude --print  (boot prompt on stdin)  → runs ONE pulse:
        a. drain new ledger lines → broadcast verified/failed/rejected to Telegram
        b. drain inbound Telegram → reconstruct thread from conversation.jsonl → reply
        c. heartbeat-check Assistant → urgent page if stale (30-min dedup)
        d. write comms heartbeat with pulse_idx
   4. record run artifacts, write fallback heartbeat, release lock, exit
```

There is nothing to "wake" — the LaunchAgent fires the process on a timer; the process does its work and dies. The lock (`~/.assistant/comms/comms-pulse.lock`) prevents a slow pulse from being double-fired by the next tick.

## Adding a second device or person

Edit `~/.assistant/comms/config.json`:

```json
{
  "telegram": { "bot_token": "...", "chat_ids": [1234567890, 9876543210] },
  "stale_heartbeat_sec": 600,
  "mute_until_epoch": 0
}
```

Picks up on the next pulse — no reload. Both chats receive every broadcast and can issue commands; confirm-back state is independent per chat. To get a new chat_id: have that user message the bot, then hit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.

## Troubleshooting

### Pulse exits non-zero / `rc=124`

```bash
tail -50 ~/.assistant/comms/comms-pulse.log
ls -t ~/.assistant/comms/pulse-runs | head -3      # recent runs
cat ~/.assistant/comms/pulse-runs/<run>/stderr.txt # "timeout after 240s" = claude ran long
```

`rc=124` is a timeout (claude exceeded 240s). One-off = a slow Bedrock response (transient — the next pulse retries). Persistent = bump `COMMS_PULSE_TIMEOUT_SEC` or trim the boot prompt. Cursors don't advance on a failed pulse, so no work is lost.

### Headless claude 403s

The pulse reuses Bedrock vars from `~/.zprofile` (launchd doesn't source it). Confirm they're exported there:
```bash
grep -E 'CLAUDE_CODE_USE_BEDROCK|AWS_REGION|AWS_BEARER_TOKEN_BEDROCK' ~/.zprofile
```
A 403 in the run dir's `stderr.txt` usually means an expired AWS token — refresh your SSO/STS session.

### `ping` / messages get no reply

- Is your chat_id in `~/.assistant/comms/config.json`? `tg-poll.py` silently drops unknown chats (anti-spam).
- Is the bot reachable? `~/.assistant/comms-venv/bin/python ~/dev/assistant/bin/tg-poll.py --timeout 1` returns JSON (maybe `[]`); errors go to stderr.
- Did a pulse actually run since you sent it? Replies wait for the next 300s tick. `launchctl kickstart -k gui/$UID/com.assistant.assistant-comms` fires one now.

### Heartbeat alert won't fire

```bash
cat ~/.assistant/heartbeat.json
```
Stale = `now - last_pulse_ts > stale_heartbeat_sec` (default 600) OR `status` ∈ `{frozen, stale_world, respawn-requested}`. Dedupes within 30 min — if you've seen one recently, the next stale tick won't re-page until the window closes.

### `restart` confirmed but Assistant didn't respawn

`restart` only back-dates Assistant's heartbeat; the respawn-on-stale logic lives in Assistant's own pulse (`bin/pulse.py`). If that LaunchAgent isn't running, use `respawn` instead (calls `spawn-assistant.sh` directly).
```bash
launchctl list | grep assistant-pulse
```

## Running the tests

```bash
cd ~/dev/assistant
~/.assistant/comms-venv/bin/python -m pytest \
  tests/test_comms_lib.py tests/test_tg_send.py tests/test_tg_poll.py \
  tests/test_threading_tools.py tests/test_conversation.py -q
```

Expected: 134 passed.

For coverage:
```bash
~/.assistant/comms-venv/bin/python -m coverage run --source=bin -m pytest \
  tests/test_comms_lib.py tests/test_tg_send.py tests/test_tg_poll.py \
  tests/test_threading_tools.py tests/test_conversation.py -q
~/.assistant/comms-venv/bin/python -m coverage report -m \
  --include='bin/comms_lib.py,bin/conversation.py,bin/tg-send.py,bin/tg-poll.py,bin/link-msg.py,bin/lookup-thread.py'
```

Expected: 100% on every comms file.

## Adding new channels later

Named `assistant-comms` (not `-telegram`) on purpose. The channel-specific code is two CLIs (`tg-send.py`, `tg-poll.py`). To add Slack/iMessage: write `slack-send.py` / `slack-poll.py` with the same JSON-line stdout contract, then mention them in the boot prompt's tool table. The threading + conversation model (`threads.jsonl`, `conversation.jsonl`, `link-msg.py`, `lookup-thread.py`) is channel-agnostic — the message id becomes whatever the channel uses.
