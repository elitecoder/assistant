# Assistant-comms — Onboarding (Slack)

**assistant-comms** watches **Assistant** and talks to you over Slack. It pings you when Assistant takes a verified action, pages you when Assistant's heartbeat goes stale, tells you the instant a workspace needs input or finishes, and lets you converse — ask "how's it going?", get a real answer — plus a recovery surface (`lesson` / `restart` / `respawn`) when Assistant is wrong.

It runs as a single **event-driven daemon**, `bin/comms-listen.py`, kept alive by a `KeepAlive` LaunchAgent (no `StartInterval` — it listens, it does not tick). The daemon runs four concurrent loops in threads:

- **Inbound** — REST-polls Slack (`conversations.history`, ~3s) for new messages in your DM/channel and feeds each to a **warm cmux Claude session** that replies in seconds.
- **Outbound pings** — watches `actions-ledger.jsonl` for appends; formats + sends each new verified/failed action. No LLM, ~2s latency floor.
- **Inbox** — watches `~/.assistant/inbox` for cmux-watcher signals ("workspace needs input" / "work complete") and pings within seconds. kqueue-driven on macOS.
- **Heartbeat page** — every 60s, checks Assistant's heartbeat; pages you (urgent, templated) if it's stale or `status ∈ {frozen, stale_world, respawn-requested}`. No LLM, 30-min dedup.

Durable memory lives entirely on disk (`conversation.jsonl` + cursors + threads), so a crash and `KeepAlive` respawn loses nothing.

## Delivery model — a private ops channel the bot owns

Comms talks over a **private Slack channel you create and invite the bot to**, using the **bot token** (`xoxb-`). A bot posting to its own ops channel is not "sending on your behalf" — it's a bot writing where it was invited, like a webhook. So the "never send Slack on my behalf" rule does not apply here, and no rule change is needed.

(DM-to-you is also supported — set the target to your `U…` id and comms opens a DM instead. Either way the target is a single channel/DM.)

## The send-gate — the bot is confined to its one channel

Even though the rule doesn't gate this, the send-gate stays as defense-in-depth so a bug or a confused warm session can't wander:

- **`slack-send.py` refuses, with no API call, any target not in `config.slack.allowed_targets`.** Setup writes that allowlist as exactly one entry: your private ops channel (or your DM). The bot physically cannot post anywhere else — the gate rejects it before egress.
- The same gate is enforced in the in-process daemon path (`src/assistant/slack.send`), so both code paths are covered.
- The bot **never** posts into any other channel or `@`-mentions third parties — its entire Slack surface is the one channel you set up.

This is the Slack analog of the Telegram/Discord comms that was removed for Adobe-IT security reasons — re-cut so the only reachable destination is the one channel the bot lives in.

## Why a warm session

Replies come from a **warm cmux Claude session** (Sonnet, scoped `--add-dir`) the daemon spawns once and keeps hot — the first reply pays the cold-start, subsequent replies land in a few seconds. The session's memory is `conversation.jsonl`, never its context window: after each reply the daemon measures context usage from the transcript, and at **≥50% of the 1M window** it **clears-and-resumes** — re-reads its boot prompt for identity, reconstructs the recent thread from disk — so a long conversation never bloats and a `/clear` loses nothing. On respawn (daemon restart, session death) the daemon closes its *own* prior warm workspace first (title-verified) so Claude processes never leak.

## What you'll see

| Trigger | Slack message |
|---|---|
| Assistant appends a verified action to its ledger | `*[cleanup]* ok `assistant:close-clean:workspace:117`` … `via=jsonl_transcript` |
| Same, but evidence is `screen_read` (Assistant rejects this as weak) | `(!)screen_read` flag in `via=` |
| A workspace needs your input / finishes work | `*<project> needs your input*  signal=`…`` |
| Assistant heartbeat stale (>10 min) or status flips to `frozen`/`stale_world`/`respawn-requested` | `*Assistant heartbeat stale*  status=frozen  last pulse 12m ago` |

Heartbeat alerts dedupe at 30 min. Messages are Slack `mrkdwn`.

## What you can text back

It's a conversation, not a verb menu. Write a full sentence ("why did it close that workspace?", "is Assistant healthy?") and comms reconstructs the recent thread from `conversation.jsonl`, reasons over Assistant's real state, and replies. Reply *in the thread* of a specific ping and comms resolves what that ping was about and answers in context.

You can also ask it to change Assistant — add a lesson, restart, respawn. Every mutation is **propose → confirm on a later message** (`y`/`yes`/`do it`), never same-turn. See the warm boot prompt (`prompts/prompt-assistant-comms-warm.md`) for the full mutation table.

## Setup (one-time)

The bot is the **same Slack app the slack-reactor already uses** (`mukuls_bot`) — reuse its `$SLACK_BOT_TOKEN`, just add the send + read scopes below.

1. **Create a private channel** (e.g. `#assistant-comms`) and **`/invite @mukuls_bot`** to it. This is the bot's one ops channel — where it pings you and where you reply. Copy its channel id (`C…`): channel name → ⌄ → About → scroll to the bottom.
2. **Scopes** — api.slack.com/apps → your app → OAuth & Permissions → Bot Token Scopes. Add:
   `chat:write`, `groups:history`, `groups:read`, `users:read`.
   (If you point the target at a DM instead of a channel, also add `im:write`, `im:history`. `reactions:*` from slack-reactor can stay.) **Reinstall** the app if scopes changed; if the token rotates, update `SLACK_BOT_TOKEN` in `~/.zprofile`.
3. **Token** — ensure `~/.zprofile` has `export SLACK_BOT_TOKEN=xoxb-…`. Optionally `export SLACK_PING_TARGET=C…` (your private channel id) to skip the interactive prompt.
4. **Run setup:**
   ```
   ./bin/assistant-comms-setup.sh
   ```
   It validates the token (`auth.test`), writes `~/.assistant/config.json` with `slack.target` (your channel) + the one-element `slack.allowed_targets` gate (chmod 600, token NOT stored), and posts a test message you should see in the channel.
5. **Load the daemon** (opt-in — load it only when you're ready):
   ```
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.assistant-comms.plist
   ```
   Or run it in the foreground: `./bin/spawn-comms-listen.sh`.

To stop: `launchctl bootout gui/$UID/com.assistant.assistant-comms`.

> The bot only sees messages in channels it's a **member** of, so the `/invite` in step 1 is required. This is the same constraint slack-reactor documents.

## Environment

| var | required | meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | `xoxb-` bot token (from `~/.zprofile`). Never stored in config.json. |
| `SLACK_PING_TARGET` | optional | `C…` private channel (recommended) or `U…` user (DMed); overrides `config.slack.target`. |
| `COMMS_MODEL` | optional | warm-session model (default Sonnet 4.6 1M). |
| `COMMS_SLACK_POLL_SEC` | optional | inbound poll interval (default 3s). |
| `COMMS_REPLY_WAIT_SEC` | optional | max wait for a warm reply (default 120s). |

## Files it owns

- `~/.assistant/config.json` — `slack.target` (your private channel) + `slack.allowed_targets` (the gate). chmod 600.
- `~/.assistant/comms/conversation.jsonl` — durable both-direction chat memory.
- `~/.assistant/comms/threads.jsonl` — sent-message-ts ↔ ledger-key links.
- `~/.assistant/comms/slack.cursor` / `ledger.cursor` — poll offsets.
- `~/.assistant/comms/session.json` — the warm workspace registry.
- `~/.assistant/comms/comms-listen.log` — the daemon's own log.

## Relationship to slack-reactor

`slack-reactor/` (Node/bolt) is a **separate, one-way** tool: react an emoji on a thread → capture it as a `/todo`. It never posts messages. assistant-comms is the **bidirectional** comms layer (pings + conversational replies), and shares only the bot app + token. The two run independently.
