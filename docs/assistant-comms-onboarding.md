# Assistant-comms — Onboarding

**assistant-comms** is a Claude session that runs in a **Mac Terminal.app window** and watches **Assistant**. It pings you on Telegram when Assistant decides or acts, and gives you a recovery surface (`restart`, `respawn`, `lesson`) when Assistant is wrong.

This is a sibling to Assistant — separate Claude session, separate cadence, separate failure modes. They share Assistant's pulse infra (every 120s) but live in different terminals: Assistant in cmux, comms in Terminal.app.

## Why Terminal and not cmux

Two reasons:

1. **You wanted to supervise comms without launching cmux.** Terminal.app is the always-available baseline.
2. **If cmux dies, Assistant dies — comms surviving means you still get the "Assistant heartbeat stale" page.** Different terminals, different blast radii.

## What you'll see

| Trigger | Telegram message |
|---|---|
| Assistant appends a verified action to its ledger | `[cleanup] ok assistant:close-clean:workspace:117 ws=workspace:117 td=- pulse=38 via=jsonl_transcript` |
| Same, but evidence is `screen_read` (Assistant rejects this) | `(!)screen_read` flag in `via=` |
| Assistant heartbeat stale (>10 min) or status flips to `frozen`/`stale_world`/`respawn-requested` | `Assistant heartbeat stale ws=workspace:18 status=frozen last pulse 12m ago` |

Heartbeat alerts dedupe at 30 min. Action pings are one-per-line, formatted by `comms_lib.fmt_action_line`.

## What you can text back

**Read-only — no confirmation needed:**
- `ping` — pong + uptime + last action age
- `last [N]` — tail N entries from the actions ledger (default 5, max 20)
- `screen` — last 50 lines of Assistant's cmux screen
- `state` — heartbeat snapshot + observer-report summary
- `mute Nm` / `mute Nh` — suppress non-urgent pings
- `unmute` — clear mute window
- `help` — verb list

**Mutating — Claude proposes, you reply `y` on the next pulse to confirm:**
- `lesson "<trigger>" "<rule>" [scope]` — append a lesson rule to `~/.claude/CLAUDE.md` via `assistant-curator.py`
- `restart` — back-date Assistant's heartbeat so the next pulse-tick respawns it
- `respawn` — run `spawn-assistant.sh` immediately (creates a fresh Assistant cmux workspace)

**Threaded replies:** if you reply to a specific Telegram message, comms looks up what that message was about (via `lookup-thread.py --tg-msg <id>`) and uses it as context for your reply.

**Anything else:** logged to `~/.assistant/comms/free-text.log` (one JSONL line) and acked. Use it as a notebook.

Mute does not silence stale-heartbeat alerts — those are urgent.

## How latency works

Comms uses Assistant's existing 120s pulse. So:

- **You text the bot → reply latency:** 0–120s, avg ~60s. The next pulse drains your message and replies.
- **Assistant takes a verified action → ping:** 0–120s. The next comms pulse reads the new ledger line.

If you want sub-10s reply latency, that's a separate tg-poll daemon — we explicitly chose not to build that. The recovery surface doesn't need real-time chat.

## Files

| Purpose | Path |
|---|---|
| Boot prompt for the Claude session | `~/dev/assistant/prompts/prompt-assistant-comms-agent.md` |
| Spawn script (opens Terminal + claude) | `~/dev/assistant/bin/spawn-comms.sh` |
| Helper lib (Paths, Config, formatting, cursors, threads) | `~/dev/assistant/bin/comms_lib.py` |
| CLI tools | `~/dev/assistant/bin/{tg-send,tg-poll,link-msg,lookup-thread}.py` |
| First-run setup | `~/dev/assistant/bin/assistant-comms-setup.sh` |
| Tests | `~/dev/assistant/tests/test_{comms_lib,tg_send,tg_poll,threading_tools}.py` |
| LaunchAgent | `~/Library/LaunchAgents/com.assistant.assistant-comms-spawn.plist` |
| Venv | `~/.assistant/comms-venv/` (only used by `assistant-comms-setup.sh`; the Claude session uses your normal `python3`) |
| Config (token + chat_ids) | `~/.assistant/comms/config.json` (chmod 600) |
| Ledger cursor | `~/.assistant/comms/ledger.cursor` |
| Telegram cursor | `~/.assistant/comms/tg.cursor` |
| Threads (sent_msg → ledger_key) | `~/.assistant/comms/threads.jsonl` |
| Comms heartbeat (Claude writes) | `~/.assistant/comms/heartbeat.json` |
| Recorded Terminal tab id | `~/.assistant/comms/terminal-tab.txt` |
| Spawn script log | `~/.assistant/comms/spawn-comms.log` |
| LaunchAgent stdout/err | `~/.architect/orchestrator-logs/assistant-comms-spawn.launchd.{out,err}` |

## Setup — one time

### 1. Prerequisites

- macOS with Python 3.13 (the comms-setup venv requires it; the running Claude session uses any `python3`).
- `uv` installed (`brew install uv`); falls back to `python3 -m venv` + `pip` if absent.
- A Telegram account.
- Assistant already set up and running (this watcher tails Assistant's existing `~/.assistant/actions-ledger.jsonl` + `~/.assistant/heartbeat.json`).
- `~/dev/assistant` repo cloned at the expected path with the comms files committed.

### 2. Make a Telegram bot

1. On Telegram, find `@BotFather`.
2. Send `/newbot`, follow prompts (name + handle ending in `_bot`).
3. BotFather returns a token like `1234567890:AAAA-...`. Keep it private.

### 3. Run the setup script

```bash
~/dev/assistant/bin/assistant-comms-setup.sh
```

It will:
1. Create `~/.assistant/comms-venv/`.
2. Install `python-telegram-bot`, `pytest`, `coverage` (used only for tests + the setup itself; the Claude session at runtime calls Telegram via the `tg-send.py` / `tg-poll.py` CLIs which use stdlib `urllib`).
3. Prompt for your bot token.
4. Wait up to 120s for you to send any message to your bot. That message captures your `chat_id`.
5. Write `~/.assistant/comms/config.json` (chmod 600).

If chat_id capture fails, send `/start` to your bot first, then re-run the script.

### 4. Smoke test by hand

Before installing the LaunchAgent, drive `spawn-comms.sh` directly:

```bash
~/dev/assistant/bin/spawn-comms.sh
```

This should:
1. Open a new Terminal.app window titled `assistant-comms`.
2. Start a `claude` session inside it.
3. Type the boot instruction: `Read ~/dev/assistant/prompts/prompt-assistant-comms-agent.md in full…`
4. Claude reads the prompt, then writes its initial heartbeat to `~/.assistant/comms/heartbeat.json`.

Wait ~10s, then on Telegram send `ping`. The next time Assistant's pulse fires (within 120s) comms will drain the inbound TG message, see your `ping`, and reply `pong, up <age>, last action <age> ago`.

If `ping` doesn't get answered within 3 minutes, see Troubleshooting.

### 5. Install the LaunchAgent

```bash
launchctl load -w ~/Library/LaunchAgents/com.assistant.assistant-comms-spawn.plist
```

Verify:
```bash
launchctl list | grep assistant-comms-spawn
# Expect: PID  0  com.assistant.assistant-comms-spawn
```

The LaunchAgent runs `spawn-comms.sh` at boot and any time the script exits non-zero (a crash). The script is idempotent — if comms is already alive, it exits 0 immediately.

To stop:
```bash
launchctl unload -w ~/Library/LaunchAgents/com.assistant.assistant-comms-spawn.plist
```

## How the loop works

Once running:

```
every 120s:  com.assistant.assistant-pulse  →  ~/dev/assistant/bin/assistant-pulse.sh
   1. drops pulse file in ~/.assistant/inbox/
   2. wakes Assistant cmux session (cmux send-text "inbox" + Enter)
   3. wakes comms Terminal tab (osascript send Enter)

inside the comms Claude session, on each Enter:
   1. drains ~/.assistant/inbox/pulse-*.json (also drained by Assistant — both can read)
   2. drains new ledger lines (~/.assistant/actions-ledger.jsonl + cursor)
   3. drains inbound TG (tg-poll.py)
   4. heartbeat-checks Assistant
   5. writes own heartbeat
```

When comms heartbeat goes stale (>600s) or its Terminal tab goes missing, `assistant-pulse.sh` calls `spawn-comms.sh` to respawn it on the next tick.

## Adding a second device or person

Edit `~/.assistant/comms/config.json`:

```json
{
  "telegram": {
    "bot_token": "...",
    "chat_ids": [1234567890, 9876543210]
  },
  "stale_heartbeat_sec": 600,
  "mute_until_epoch": 0
}
```

The change picks up on the next pulse — no restart needed. Both chats receive every broadcast and can issue commands. Confirm-back state is independent per chat.

To find a new chat_id: have that user send any message to the bot, then hit `https://api.telegram.org/bot<TOKEN>/getUpdates` from a browser and read the JSON.

## Troubleshooting

### Terminal didn't open when LaunchAgent loaded

```bash
tail -50 ~/.architect/orchestrator-logs/assistant-comms-spawn.launchd.err
tail -50 ~/.assistant/comms/spawn-comms.log
```

Common causes:
- AppleScript hasn't been granted Terminal-automation permission. macOS will pop up a one-time consent dialog the first time `osascript` controls Terminal.app — you have to click Allow.
- `~/.assistant/comms/config.json` is missing → re-run `assistant-comms-setup.sh`.
- `~/dev/assistant/prompts/prompt-assistant-comms-agent.md` doesn't exist → did you pull the right git branch?

### `ping` returns nothing

In the comms Terminal window, you should see Claude is taking pulse turns every ~120s. If the window is silent:
- Did `spawn-comms.sh` deliver the boot instruction? Look for `Read ~/dev/assistant/prompts/prompt-assistant-comms-agent.md` in the Terminal scrollback.
- Is your chat_id in `~/.assistant/comms/config.json`? `tg-poll.py` silently drops messages from unknown chats (anti-spam).
- Is the bot reachable? `~/.assistant/comms-venv/bin/python ~/dev/assistant/bin/tg-poll.py --timeout 1` should return JSON (possibly `[]`); errors print to stderr.

### Heartbeat alert won't fire

```bash
cat ~/.assistant/heartbeat.json
```

Stale = `now - last_pulse_ts > stale_heartbeat_sec` (default 600) OR `status` ∈ `{frozen, stale_world, respawn-requested}`. Alerts dedupe within 30 min — if you've seen one in the last 30 min, the next stale tick won't re-page until the window closes.

### Comms keeps respawning (crash loop)

```bash
launchctl list | grep assistant-comms-spawn
# Look at the third column: 0 means clean exit, non-zero means crash
tail -100 ~/.architect/orchestrator-logs/assistant-comms-spawn.launchd.err
```

If `spawn-comms.sh` exits non-zero faster than `ThrottleInterval` (30s), launchd backs off. Common cause: `claude` itself fails to launch (auth, model unavailable, bad prompt path) — the script logs it.

### Pulse not waking the Terminal tab

```bash
tail -100 ~/.assistant/assistant-pulse.log | grep comms
```

If you see `comms: tab file empty` repeatedly, the recorded tab id was lost (e.g. you closed the Terminal window). The next pulse will run `spawn-comms.sh` to re-create it.

If you see `comms: osascript wake failed`, the recorded tab id is stale (Terminal restarted, tab id reissued). `spawn-comms.sh` detects this on its alive-check and respawns.

### `restart` confirmed but Assistant didn't respawn

`restart` only back-dates Assistant's heartbeat. The respawn-on-stale-heartbeat behaviour lives in `~/dev/assistant/bin/assistant-pulse.sh`. If that LaunchAgent isn't running, `restart` does nothing — use `respawn` instead, which calls `spawn-assistant.sh` directly.

```bash
launchctl list | grep assistant-pulse
```

## Running the tests

```bash
cd ~/dev/assistant
~/.assistant/comms-venv/bin/python -m pytest tests/test_comms_lib.py tests/test_tg_send.py tests/test_tg_poll.py tests/test_threading_tools.py -q
```

Expected: 115 passed.

For coverage:
```bash
cd ~/dev/assistant
~/.assistant/comms-venv/bin/python -m coverage run --source=bin -m pytest \
  tests/test_comms_lib.py tests/test_tg_send.py tests/test_tg_poll.py tests/test_threading_tools.py -q
~/.assistant/comms-venv/bin/python -m coverage report -m \
  --include='bin/comms_lib.py,bin/tg-send.py,bin/tg-poll.py,bin/link-msg.py,bin/lookup-thread.py'
```

Expected: 100% on every comms file.

## Adding new channels later

Comms was named `assistant-comms` (not `assistant-telegram`) on purpose. The channel-specific code is concentrated in two CLIs (`tg-send.py`, `tg-poll.py`). To add Slack or iMessage:

1. Write `slack-send.py` / `slack-poll.py` with the same JSON-line stdout contract.
2. Update the boot prompt's verb table to mention the new send/poll tools.
3. The Claude session decides per pulse which channel to broadcast to (e.g. all channels for urgent, only Slack for routine).

The threading model (`threads.jsonl`, `link-msg.py`, `lookup-thread.py`) is channel-agnostic — `tg_msg_id` becomes whatever the channel's message identifier is.
