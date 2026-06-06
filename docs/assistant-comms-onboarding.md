# Assistant-comms — Onboarding

**assistant-comms** watches **Assistant** and talks to you over Telegram. It pings you when Assistant takes a verified action, pages you when Assistant's heartbeat goes stale, and lets you converse — ask "how's it going?", get a real answer — plus a recovery surface (`lesson` / `restart` / `respawn`) when Assistant is wrong.

It runs as a single **event-driven daemon**, `bin/comms-listen.py`, kept alive by a `KeepAlive` LaunchAgent (no `StartInterval` — it listens, it does not tick). The daemon runs three concurrent loops in threads:

- **Inbound** — Telegram long-poll (`getUpdates`, 25s) returns the instant you message, so there's no queue wait. It feeds your message to a **warm cmux Claude session** that replies in seconds.
- **Outbound pings** — watches `actions-ledger.jsonl` for appends; formats + sends each new verified/failed action. No LLM, ~2s latency floor.
- **Heartbeat page** — every 60s, checks Assistant's heartbeat; pages you (urgent, templated) if it's stale or `status ∈ {frozen, stale_world, respawn-requested}`. No LLM, 30-min dedup.

Durable memory lives entirely on disk (`conversation.jsonl` + cursors + threads), so a crash and `KeepAlive` respawn loses nothing.

## Why a warm session

Replies come from a **warm cmux Claude session** (Sonnet, scoped `--add-dir`) that the daemon spawns once and keeps hot — first reply pays the cold-start, subsequent replies land in ~2.6s. The session's memory is `conversation.jsonl`, never its context window: after each reply the daemon measures context usage from the transcript, and at **≥50% of the 1M window** it **clears-and-resumes** the session — re-reads its boot prompt for identity, reconstructs the recent thread from disk — so a long conversation never bloats and a `/clear` loses nothing. On respawn (daemon restart, session death) the daemon closes its *own* prior warm workspace first (title-verified) so Claude processes never leak.

> An earlier design ran comms as a 300s headless `claude --print` one-shot pulse (`comms-pulse.py`). It was replaced by this event-driven daemon to kill the up-to-5-minute reply latency. If you find references to `comms-pulse.py` anywhere, they're stale.

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

**Mutating Assistant — the warm session proposes, you reply `y` on a later turn to confirm:**
- `lesson "<trigger>" "<rule>" [scope]` — append a lesson rule via `assistant-curator.py`
- `restart` — back-date Assistant's heartbeat so its pulse respawns it
- `respawn` — run `spawn-assistant.sh` immediately (fresh Assistant cmux workspace)

The propose and the confirm happen on **different turns** — the warm session never runs a mutation in the same turn it proposes it. A `/clear` (or crash) between the two simply drops the pending proposal, so a restart never auto-fires; you just re-ask. Mute never silences stale-heartbeat alerts.

## How latency works

The daemon is event-driven, so there's no queue wait:

- **You text the bot → reply:** the Telegram long-poll returns the instant you message; the warm session replies in ~2.6s once hot (the first reply after a spawn pays the Claude cold-start, ~30–90s).
- **Assistant takes a verified action → ping:** the ledger watcher polls every ~2s and fires the formatted line with no LLM in the path.
- **Heartbeat page:** checked every 60s, deduped to one page per 30 min.

This is a recovery-and-reporting surface that happens to be fast enough to converse over. Durable memory (`conversation.jsonl`) gives continuity across `/clear` and crashes; the event loops give latency.

## Files

| Purpose | Path |
|---|---|
| Warm-session boot prompt (identity; survives a clear-and-resume) | `~/dev/assistant/prompts/prompt-assistant-comms-warm.md` |
| The daemon (long-poll inbound + ledger watch + heartbeat timer; flock singleton) | `~/dev/assistant/bin/comms-listen.py` |
| Warm cmux session lifecycle (spawn Sonnet / feed / read reply / clear-at-50% / no-leak respawn) | `~/dev/assistant/bin/comms_session.py` |
| Helper lib (Paths, Config, formatting, cursors, threads, conversation, context-measure) | `~/dev/assistant/bin/comms_lib.py` |
| CLI tools | `~/dev/assistant/bin/{tg-send,tg-poll,link-msg,lookup-thread,conversation}.py` |
| First-run setup | `~/dev/assistant/bin/assistant-comms-setup.sh` |
| Tests | `~/dev/assistant/tests/test_{comms_lib,tg_send,tg_poll,threading_tools,conversation}.py` |
| LaunchAgent (KeepAlive; no StartInterval) | `~/Library/LaunchAgents/com.assistant.assistant-comms.plist` (repo: `launchagents/`) |
| Venv (setup + tests only; the daemon uses system `python3` + `claude`) | `~/.assistant/comms-venv/` |
| Config (token + chat_ids) | `~/.assistant/comms/config.json` (chmod 600) |
| Durable chat memory | `~/.assistant/comms/conversation.jsonl` |
| Ledger / Telegram cursors | `~/.assistant/comms/{ledger.cursor,tg.cursor}` |
| Threads (sent_msg → ledger_key) | `~/.assistant/comms/threads.jsonl` |
| Comms heartbeat (daemon writes) | `~/.assistant/comms/heartbeat.json` |
| Singleton lock (flock pidfile) | `~/.assistant/comms/comms-listen.pid` |
| Daemon log (every loop event) | `~/.assistant/comms/comms-listen.log` |
| LaunchAgent stdout/err | `~/.architect/orchestrator-logs/assistant-comms.launchd.{out,err}` |

## Setup — one time

### 1. Prerequisites

- macOS with Python 3.13 (the setup venv needs it; the daemon uses system `python3` + the `claude` CLI).
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
2. Install `python-telegram-bot` (for the test suite; the daemon's Telegram calls use stdlib `urllib` via `tg-send.py` / `tg-poll.py`).
3. Prompt for your bot token.
4. Wait up to 120s for you to send any message to your bot — that captures your `chat_id`.
5. Write `~/.assistant/comms/config.json` (chmod 600).

If chat_id capture fails, send `/start` to your bot first, then re-run.

### 4. Smoke test by hand

Run the daemon in the foreground — no LaunchAgent yet (Ctrl-C to stop):

```bash
/opt/homebrew/bin/python3 ~/dev/assistant/bin/comms-listen.py
```

You'll see all three loops start, then the warm session spawn (~30–90s for the first cold start). Send your bot `ping` — expect a `pong` reply on your phone within a few seconds. Inspect what it did:

```bash
cat ~/.assistant/comms/heartbeat.json          # expect status=active, note=listen-daemon
tail -20 ~/.assistant/comms/comms-listen.log   # per-loop events
```

If the daemon exits immediately, the most common cause is a missing `config.json` (run the setup script) or a missing warm prompt — both are logged on exit.

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
# Expect a nonzero PID (the daemon is long-running), not "-":  55333  0  com.assistant.assistant-comms
```

To stop / start manually:
```bash
launchctl bootout  gui/$UID/com.assistant.assistant-comms
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.assistant-comms.plist

# Restart the daemon on demand (KeepAlive relaunches it):
launchctl kickstart -k gui/$UID/com.assistant.assistant-comms
```

## How the loop works

```
com.assistant.assistant-comms (KeepAlive)  →  bin/comms-listen.py
   on start:  acquire flock singleton (a second daemon exits — two long-pollers
              against one bot token collide with Telegram 409), merge Bedrock env,
              spawn the warm cmux session, start three threads:

   INBOUND   long-poll getUpdates(25s) → on message: feed the warm session,
             read its reply from the transcript, tg-send, clear-and-resume at ≥50% ctx
   LEDGER    poll actions-ledger.jsonl (~2s) → tg-send each new line (no LLM)
   HEARTBEAT every 60s: page if Assistant heartbeat stale / status bad (30-min dedup);
             also writes the comms heartbeat each tick
```

There is nothing to "wake" — the process listens. `KeepAlive` relaunches it if it crashes or exits; the flock pidfile (`~/.assistant/comms/comms-listen.pid`) ensures only one daemon runs at a time and auto-releases when the holder dies, so a crash never leaves a stuck lock.

## Adding a second device or person

Edit `~/.assistant/comms/config.json`:

```json
{
  "telegram": { "bot_token": "...", "chat_ids": [1234567890, 9876543210] },
  "stale_heartbeat_sec": 600,
  "mute_until_epoch": 0
}
```

Picks up when the daemon next reads config (it re-reads each loop iteration) — no reload needed. Both chats receive every broadcast and can issue commands; confirm-back state is independent per chat. To get a new chat_id: have that user message the bot, then hit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.

## Troubleshooting

### Daemon won't stay up / exits immediately

```bash
tail -50 ~/.assistant/comms/comms-listen.log
tail -20 ~/.architect/orchestrator-logs/assistant-comms.launchd.err
```

The daemon exits non-zero (and `KeepAlive` relaunches it every ~10s) when `config.json` is missing (run the setup script) or the warm prompt is missing. A second daemon instance exits cleanly with "another comms-listen already holds the lock" — that's the flock singleton, not an error.

### Warm-session reply 403s / times out

The daemon reuses Bedrock vars from `~/.zprofile` (launchd doesn't source it). Confirm they're exported there:
```bash
grep -E 'CLAUDE_CODE_USE_BEDROCK|AWS_REGION|AWS_BEARER_TOKEN_BEDROCK' ~/.zprofile
```
A 403 from the warm session usually means an expired AWS token — refresh your SSO/STS session. A reply that never lands within `COMMS_REPLY_WAIT_SEC` (default 120) shows up in `comms-listen.log`; a slow first reply is usually just the warm session's cold start.

### `ping` / messages get no reply

- Is your chat_id in `~/.assistant/comms/config.json`? `tg-poll.py` silently drops unknown chats (anti-spam).
- Is the bot reachable? `~/.assistant/comms-venv/bin/python ~/dev/assistant/bin/tg-poll.py --timeout 1` returns JSON (maybe `[]`); errors go to stderr.
- Is the daemon actually running? `launchctl list | grep assistant-comms` should show a nonzero PID. If not, `launchctl kickstart -k gui/$UID/com.assistant.assistant-comms` restarts it.
- Is the warm session alive? `tail comms-listen.log` for `warm session ready`. A dead session is respawned on the next inbound message.

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

Expected: 149 passed.

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
