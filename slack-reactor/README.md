# slack-reactor

React with this machine's emoji on **any** Slack thread → capture the whole
thread as a `/todo` item in `~/.claude/assistant-todo.json` (the same store the
`/todo` skill, `pulse.py`, and the dashboard read).

Built on [Perch](https://github.com/elitecoder/perch)'s `@slack/bolt` pattern,
but tuned for two hard requirements:

### 1. No per-channel bot invites

The reaction is delivered as a **user event**, not a bot event:

| | bot events (what Perch uses) | **user events (this)** |
|---|---|---|
| manifest key | `event_subscriptions.bot_events` | `event_subscriptions.user_events` |
| scope location | `oauth_config.scopes.bot` | `oauth_config.scopes.user` |
| token | bot `xoxb-` | **your user `xoxp-`** |
| reach | only channels the **bot** joined | **every channel YOU can see** (public, private, DMs) |
| invites | one per channel | **none, ever** |

We subscribe to `reaction_added` under `user_events` with the **user** scope
`reactions:read`, authorized by your user token. Slack delivers the event for
anything the authorizing user can see — no bot member in the channel.

### 2. Per-machine routing

**One Slack app per machine.** A Slack app has exactly one Events Request URL,
which points at exactly one machine's tunnel. Each machine claims one emoji via
`$TODO_EMOJI` and ignores reactions whose name differs — so other emojis on
other machines' apps route there.

```
this machine (Mukuls-MacBook-Pro)   →  app "reactor-mukuls-mbp"  →  TODO_EMOJI=mukuls2
another machine                     →  app "reactor-other"       →  TODO_EMOJI=...
```

## Transport

bolt in **HTTP mode** (`ExpressReceiver`) behind the existing cloudflared tunnel
(`com.assistant.cloudflared-tunnel`). **Socket Mode is intentionally NOT used** —
it delivers bot events + interactivity, but not user-token events, which this
design depends on. The tunnel already gives us the public Request URL Slack needs.

## Feedback

A `:white_check_mark:` reaction only (via the bot token if present, else the user
token). It never `chat.postMessage`s — operator rule: never send Slack messages
on the user's behalf.

## Environment

| var | required | meaning |
|---|---|---|
| `SLACK_USER_TOKEN` | ✅ | `xoxp-` user token; user scopes `reactions:read` + `*:history`. Reads run as you. |
| `SLACK_SIGNING_SECRET` | ✅ | Basic Information → Signing Secret. Verifies Slack's POSTs. |
| `SLACK_BOT_TOKEN` | optional | `xoxb-`, scope `reactions:write`, for the ✅ react-back. Falls back to user token. |
| `TODO_EMOJI` | default `mukuls2` | emoji name(s) this machine claims (comma-separated for several). |
| `SLACK_REACTOR_PORT` | default `3737` | local HTTP port bolt listens on (tunnel forwards here). |
| `TODO_PRIORITY` | default `P2` | priority for captured todos. |
| `TODO_AUTODISPATCH` | default `1` | `0` = manual-only (no auto-dispatch). |

Tokens live in `~/.zprofile`; the launcher sources it. Nothing is hardcoded.

## Setup (one-time, per machine)

1. **Create a per-machine Slack app** from `slack/manifest.json`:
   - api.slack.com → Create App → **From a manifest** → paste `slack/manifest.json`.
   - Replace `REPLACE-HOSTNAME` (app name) and `REPLACE-WITH-TUNNEL-HOSTNAME`
     (your cloudflared hostname) first.
2. **Install to workspace** → grant the user scopes → copy the **User OAuth
   Token** (`xoxp-`) into `~/.zprofile` as `SLACK_USER_TOKEN`.
3. Copy **Signing Secret** (Basic Information) into `~/.zprofile` as
   `SLACK_SIGNING_SECRET`.
4. Add a cloudflared ingress rule mapping the app's hostname →
   `http://localhost:3737`, and set the manifest's `request_url` to
   `https://<that-hostname>/slack/events`.
5. `export TODO_EMOJI=mukuls2` in `~/.zprofile` (this machine's emoji).
6. Load the LaunchAgent: `./install.sh --apply` (or
   `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.slack-reactor.plist`).
7. Slack will verify the Request URL on save — the daemon must be running.

## Run manually

```bash
node slack-reactor/src/index.js          # or: ./bin/spawn-slack-reactor.sh
```

## Test the write path (no Slack needed)

```bash
HOME=/tmp/reactor-home node --input-type=module -e "
  import { addTodo } from './slack-reactor/src/todo-store.js';
  console.log(addTodo({title:'t', detail:'d', url:'u', source:'slack-react:C:1.1'}));
"
```
