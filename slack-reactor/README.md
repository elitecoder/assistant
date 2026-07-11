# slack-reactor

React with this machine's emoji on a Slack thread → capture the whole thread as
a `/todo` item in `~/.claude/assistant-todo.json` (the same store the `/todo`
skill, `pulse.py`, and the dashboard read).

Built on [Perch](https://github.com/elitecoder/perch)'s `@slack/bolt` + Socket
Mode pattern.

## Transport: Socket Mode (bot events)

bolt in **Socket Mode** — `SLACK_BOT_TOKEN` (`xoxb-`) + `SLACK_APP_TOKEN`
(`xapp-`). **No public URL, no signing secret.**

The trade-off this buys: Socket Mode delivers **bot** events, and a bot only
receives `reaction_added` for channels it is a **member** of. **So you must
`/invite` the bot to each channel** you want capture to work in. (The
alternative — user-token events that fire for every channel you can see, no
invites — needs a public HTTPS Request URL. See "History" below.)

## Per-machine routing

Each machine claims one emoji via `$TODO_EMOJI` and ignores reactions whose name
differs, so other emojis route to other machines. Two ways to run several
machines:

- **Shared bot app**, each machine sets a different `$TODO_EMOJI`. Simplest;
  every machine's daemon sees every reaction but only acts on its own emoji.
- **One app per machine** (separate `xapp-`/`xoxb-` pair each). Stronger
  isolation. Use if machines shouldn't share a bot identity.

This machine: `TODO_EMOJI=mukuls2`.

## Feedback

A `:white_check_mark:` reaction only (via the bot token). It never
`chat.postMessage`s — operator rule: never send Slack messages on the user's
behalf.

## Environment

| var | required | meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | `xoxb-`; bot scopes `reactions:read`, `reactions:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history`. |
| `SLACK_APP_TOKEN` | ✅ | `xapp-` app-level token, scope `connections:write` (Socket Mode). |
| `SLACK_USER_ID` | optional | If set, only this user's reactions create todos (e.g. your `U…`). |
| `TODO_EMOJI` | default `mukuls2` | emoji name(s) this machine claims (comma-separated for several). |
| `TODO_PRIORITY` | default `P2` | priority for captured todos. |
| `TODO_AUTODISPATCH` | default `1` | `0` = manual-only (no auto-dispatch). |

Tokens live in `~/.zprofile`; the launcher sources it. Nothing is hardcoded.

## Setup (one-time)

Add this to the **existing `mukuls_bot` app** (don't create a new one — keeps the
bot token that's already in `$SLACK_BOT_TOKEN`).

1. api.slack.com/apps → **mukuls_bot** → **OAuth & Permissions → Bot Token
   Scopes** → add `reactions:read`, `groups:history`, `im:history`,
   `mpim:history` (it already has `reactions:write`, `channels:history`).
2. **Socket Mode** → toggle **Enable Socket Mode**.
3. **Basic Information → App-Level Tokens** → generate a token with scope
   `connections:write` → copy the `xapp-…`.
4. **Event Subscriptions** → enable → under **Subscribe to bot events** add
   `reaction_added` → save.
5. **Reinstall** the app (scope change) → the bot token may rotate; if so update
   `SLACK_BOT_TOKEN` in `~/.zprofile`.
6. `~/.zprofile`: `export SLACK_APP_TOKEN=xapp-…`, `export TODO_EMOJI=mukuls2`,
   and optionally `export SLACK_USER_ID=U…`.
7. `/invite @mukuls_bot` in every channel you want capture to work in.
8. Load the daemon: `./install.sh --apply` (or `launchctl bootstrap gui/$UID
   ~/Library/LaunchAgents/com.assistant.slack-reactor.plist`).

## World-event feed (M5 wave-2) + scope

Besides the emoji→TODO reactor, this app feeds the read-only
`bin/connectors/slack-events.py` connector: `app_mention` and `message.im`
events are spooled (atomic, `0600`) into
`~/.assistant/connectors/slack/spool/`, and the Python connector normalizes them
into WorldEvents. The reactor still **never** sends a message.

**Default scope = @-mentions + DMs only** (design §9 — privacy, not a channel
firehose). The manifest subscribes to `app_mention` + `message.im` only. A
channel @-mention arrives via `app_mention` **only** (no mirror `message`
event), which is also why there is no nondeterministic escalate-vs-digest
swallow.

- **Opt into the channel firehose** (not recommended): add `message.channels`
  and `message.groups` back to `bot_events` in `slack/manifest.json`, **re-import
  the manifest and reinstall** (see below), and set
  `export SLACK_INGEST_CHANNELS=1` in `~/.zprofile`.
- **Spool hygiene:** files are `0600`; both the reactor (hourly + on-write) and
  the connector reap orphaned `.tmp` files, age-prune past 7 days, and cap the
  file count — so the spool stays bounded even if only the reactor is running.
  Tunables: `SLACK_SPOOL_MAX_FILES`, `SLACK_SPOOL_MAX_AGE_MS` (reactor) /
  `spool_max_files`, `spool_max_age_days` (connector config).

## Re-import the manifest (REQUIRED after any scope/event change)

Editing `slack/manifest.json` (e.g. adding `message.channels`) does **nothing**
until you push it to Slack and reinstall — otherwise the added scope/event is
never granted and **the connector silently produces nothing**:

1. api.slack.com/apps → **mukuls_bot** → **App Manifest** → paste the updated
   `slack/manifest.json` → **Save Changes**.
2. **Install App** (or **OAuth & Permissions → Reinstall to Workspace**) →
   approve the **OAuth re-consent** for the added scope.
3. If the bot token rotated on reinstall, update `SLACK_BOT_TOKEN` in
   `~/.zprofile` and restart the daemon.
4. `/invite @mukuls_bot` into any newly-relevant channels.

Skipping step 1–2 is the classic silent failure: the code subscribes to an event
Slack was never told to deliver.

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

## History

The first cut used **user-token events** (`user_events` + `scopes.user`) to avoid
per-channel invites — events fire for every channel the authorizing user can
see. That requires a public HTTPS **Request URL** (Socket Mode does not deliver
user events), and we had no working public endpoint to host it. We switched to
Socket Mode + bot events, accepting the per-channel `/invite` requirement in
exchange for needing no public URL. If a durable public endpoint is set up
later, the user-events design (no invites) can return.
