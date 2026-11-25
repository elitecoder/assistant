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
| `SLACK_USER_ID` | strongly recommended | Your own Slack user id: `U` or `W` followed by capital letters and digits (in Slack, open your profile, choose the three dots, then Copy member ID). When the owner is known, only your reactions are captured at all. A value of any other shape is reported in the log and ignored, and the owner is then unknown. See "Who can start work". |
| `TODO_EMOJI` | default `mukuls2` | emoji name(s) this machine claims (comma-separated for several). |
| `TODO_PRIORITY` | default `P2` | priority for captured todos. |
| `TODO_AUTODISPATCH` | default `1` | `0` = manual-only (no auto-dispatch), for everyone. |

Tokens live in `~/.zprofile`; the launcher sources it. Nothing is hardcoded.

## Who can start work

Text written by someone else can prepare work; only the owner's own action
starts it. The reactor works out who the owner is once, at start, and prints
one line saying how, with the id it found (a user id is not a secret). It takes
the first of these:

1. `SLACK_USER_ID`, when it has the shape of a user id. A typo, a channel id or
   two ids are reported in the log (the value is not printed) and the owner is
   unknown.
2. `slack.owner` in `~/.assistant/config.json`.
3. `slack.target` in `~/.assistant/config.json`, when it starts with `U` or `W`.
   That is where notifications go, and the Slack channel setup writes it, so it
   is taken as you only when `slack.owner` and `SLACK_USER_ID` are both absent.
   It must be your own id; if your notifications go to someone else, set
   `SLACK_USER_ID`.

- **Owner known:** your reaction creates the task and it starts by itself (unless
  `TODO_AUTODISPATCH=0`). A reaction from anyone else is ignored.
- **Owner unknown, Pro mode, and `SLACK_USER_ID` not set at all (or empty):**
  every member's reaction is captured as a plain task and starts by itself, as it
  did before the owner rule (unless `TODO_AUTODISPATCH=0`): the normal priority,
  no title prefix, the check mark reaction, no limit on how many wait, and the
  log line says `owner unknown: captured as before the owner rule`. Anyone in a
  channel the bot is in can start a task this way until you set `SLACK_USER_ID`;
  the start-up line says so, and so does the doctor when your Slack target is a
  channel. A `SLACK_USER_ID` that is set but isn't a user id (a typo, spaces) is
  not this case: it behaves as in the default mode, below. The reactor is in Pro
  mode only when `bin/assistant-mode.py` prints `pro`; it asks when it handles a
  reaction (and not at all when `mode` in `~/.assistant/config.json` is saved as
  something other than `pro`), and keeps the answer for a minute. If it can't be
  asked, the install is treated as the default mode, with one line in the log.
- **Owner unknown, default mode:** every reaction is captured, so nothing you react to is lost,
  but as an **Ask me** task that never starts by itself, whatever
  `TODO_AUTODISPATCH` says. The card's title starts with "Not started, reaction
  not confirmed as yours: ", the priority is P3, the reactor puts an `eyes`
  reaction on the message (not the check mark), and the log line says
  `Ask me: owner unknown`. No more than 20 such tasks wait at once; after that
  further reactions are skipped, with one log line each. The detail starts with
  what to do: copy your member ID in Slack, add `export SLACK_USER_ID=U…` to
  `~/.zprofile`, and run
  `launchctl kickstart -k gui/$UID/com.assistant.slack-reactor`. Press **Queue
  to start** to start a task now. A task already saved as Ask me stays that way
  until you press **On**.

The thread in a task's detail is untrusted text, so it sits between
`----- BEGIN UNTRUSTED THREAD <code> -----` and `----- END UNTRUSTED THREAD <code> -----`
(a new random code for each task), under rules telling the agent to treat it as
content to act on and never as instructions; text that reads as a marker is
replaced. The reactor reads the whole thread, a page of 200 messages at a time
(up to 10 pages; the detail says when it read only the first part). A long thread
keeps its first message and the message you reacted to (marked `[reacted to]`),
then the newest that fit in 50 messages and 8,000 characters; a line inside the
fence says how many were left out. The link to the thread stays outside the
fence, and the dispatched agent's prompt shows a fixed title instead of the first
message. A Slack task saved without a fence (by an earlier version) is fenced
when its prompt is built.

Not covered here:

- A task from a reaction runs under the full dispatch rules, not the Work queue's
  first-pass limits.
- The agent is given the thread's link and may read the thread from Slack
  itself; what it reads there is not fenced.

## The task list lock

Every write the reactor makes to `~/.claude/assistant-todo.json` happens while holding the same
lock the Python services take (`~/.claude/assistant-todo.lock`; the `/todo` and `/cleanup` skills don't yet). On macOS, opening
that file with `O_EXLOCK` takes the very lock `flock()` takes, so the reactor
needs no extra software. It retries for up to 5 seconds without blocking, and if
it still can't get the lock it tries the whole add again, three tries about 15
seconds apart, so other reactions are handled meanwhile. If all fail (or it isn't
on macOS) it saves nothing, logs `todo write failed`, and puts a `warning`
reaction on the message so you can see it wasn't saved. The temporary file has a name of its own, mode `0600`, and
is removed if the rename fails.

## Tokens in the log

**If the reactor ran any earlier version, rotate your Slack bot token now: its
log file holds the old one.** The fix stops new leaks; it doesn't clean old
lines.

1. In the Slack app's settings, under OAuth & Permissions, reinstall the app
   to get a new bot token.
2. If the old log also shows an `xapp-` token, regenerate the app-level token
   under Basic Information, App-Level Tokens.
3. Update `SLACK_BOT_TOKEN` (and `SLACK_APP_TOKEN`) in `~/.zprofile`.
4. Restart the reactor with this command:
   `launchctl kickstart -k gui/$(id -u)/com.assistant.slack-reactor`
5. Confirm the old token no longer works. Call `auth.test` with it, for
   example with this command, and expect `invalid_auth` or `token_revoked`:
   `curl -s -X POST -H "Authorization: Bearer <old token>" https://slack.com/api/auth.test`
6. Delete or empty `~/.assistant/logs/slack-reactor.launchd.err`, and any copy
   of it in a backup.

After that, what the reactor prints, and what it prints when it crashes, has
anything shaped like a secret replaced with `<redacted>`: Slack tokens, `Bearer`
and `Authorization` values, cookies, `token=`, `password=` and `api_key=` fields,
GitHub, Google, AWS and model keys, JWTs, and web addresses with a password in
them. When something fails it prints one line (the error's name and message, its
code, the HTTP status, Slack's reason, the network error underneath such as
ENOTFOUND or ETIMEDOUT, and one level of cause). A crash adds the time and up to
8 stack lines, so you can tell where it failed, and exits non-zero so launchd
restarts the reactor as before. An error logged through `console` gets at most 3
stack lines. `src/index.js` loads `safe-output-boot.js` before anything else;
keep it first.

This is pattern matching, not a guarantee. It does not cover output written with
`fs.writeSync` or `process._rawDebug`, a token split across two writes, or a
secret in a shape the rules don't know.

Run the reactor's tests with `node --test slack-reactor/test/*.test.js`. To
measure coverage you need Node 22 or newer and an output folder that exists:

```bash
mkdir -p /tmp/reactor-coverage
node --test --experimental-test-coverage --test-reporter=spec --test-reporter-destination=stdout \
  --test-reporter=lcov --test-reporter-destination=/tmp/reactor-coverage/reactor.lcov \
  slack-reactor/test/*.test.js
```

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
   and `export SLACK_USER_ID=U…` (your own Slack user id; without it nothing
   you react to starts by itself).
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
mkdir -p /tmp/reactor-home/.claude && echo '{"items":[]}' > /tmp/reactor-home/.claude/assistant-todo.json
HOME=/tmp/reactor-home node --input-type=module -e "
  import { addTodo } from './slack-reactor/src/todo-store.js';
  console.log(await addTodo({title:'t', detail:'d', url:'u', source:'slack-react:C:1.1'}));
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
