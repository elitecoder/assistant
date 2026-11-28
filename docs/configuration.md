# Configuration

Assistant ships with safe defaults: silent, draft-only, and no unattended goal
work. Every setting below is a deliberate edit you make yourself.

| Setting | Default | Where | What it does |
|---|---|---|---|
| Noise budget | `page:0, notify:0` (fully silent) | `~/.assistant/noise-budget.json` | Maximum pushes a day through the interrupt gate. A `page` also requires lane `escalate`, urgency `now`, and a policy marked `pageable`. |
| Brief wake hour | `7` (local time) | `~/.assistant/comms/config.json` → `{"brief": {"wake_hour": N}}` | The first pulse at or after this hour builds the day's brief, when the brief is [turned on](#background-jobs-that-are-off-by-default). |
| Planner autoDispatch | Off (and the planner itself is [off by default](#background-jobs-that-are-off-by-default)) | `~/.assistant/comms/config.json` → `{"planner": {"autoDispatch": true}}` | Off: a stalled goal stages a decision for you to review. On: whitelisted steps dispatch unattended. Turn it on only after you've watched the planner behave. |
| Slack channel ingest | Mentions and DMs only | `SLACK_INGEST_CHANNELS=1` plus a manifest edit | Opts in to full channel message ingestion. See [Connect your sources](connectors.md#slack-read-only). |
| Slack focus list | Not set up | `slack_focus` block in `~/.assistant/comms/config.json` plus `SLACK_USER_TOKEN` | Which channels, bots, repos, and Jira projects the Slack tab uses. See [Slack focus list](connectors.md#slack-focus-list). |
| Connector cadence and caps | 60 s, 200 events, 10 pages | `connectors` block in `~/.assistant/comms/config.json` | Per-connector poll interval and batch caps. The module constants are only the fallback. |
| Coding agent | Claude | `bin/assistant-llm.py set claude\|copilot\|droid\|local [--feature <name>]` | Chooses Claude Code, GitHub Copilot CLI, Factory Droid, or a local model. Without `--feature`, it sets `llm.provider`, which every launch follows: task sessions, the Slack reply session, resumes, `/spawn-claude-workspace`, and the background model jobs (`triage`, `observer`, `observer_audit`, `strategist`, `narrator`, `lessons`, `slack_judge`, `work_queue`). `--feature` picks one job only; `--feature comms` picks the Slack session's agent. With Copilot picked, FFP to-dos still go to Claude. `/spawn-claude-workspace` keeps its own default (`droid`) unless you name an `engine`. If the chosen agent isn't installed, task and Slack sessions start in Claude. `set copilot` also writes cmux's Copilot hooks to `~/.copilot/hooks/cmux.json` so Copilot sessions show on the dashboard and can be restored. Copilot reaches the `/cleanup` and `/merge-when-ready` skills only if `~/.copilot/skills` links to `~/.claude/skills`. `bin/assistant-llm.py status` shows the current routing. |
| Claude alias | `claude` | `bin/assistant-llm.py claude-alias <name>` (saves `llm.claude.alias` in `~/.assistant/comms/config.json`) | The shell alias Claude sessions start with: task sessions, `/spawn-claude-workspace`, and the Slack session. The workspace's own shell runs it, so cmux's Claude wrapper still records the session; if that shell doesn't define the alias, the session starts with `claude` and `assistant-doctor.py` warns. Every restore, by the Assistant or by cmux after a restart or a hibernation wake, replays cmux's saved command, which skips the alias. To keep restored sessions on the alias, declare the alias's script in cmux's [`agents.launchers`](https://cmux.com/docs/configuration#cmux-json) setting; cmux spots the script only when it runs claude as a child process under its own name. The command refuses a name your login zsh doesn't know, a shell keyword such as `if` or `exec`, and `copilot` or `droid`. `claude-alias claude` restores the default. A live Slack session restarts on its next message after the alias changes. The Slack session takes on the alias's own folders and permission mode; the Assistant's model choice still wins. A hand-edited alias that names a shell builtin such as `echo` starts no session, and `assistant-doctor.py` warns. Background model jobs still run the Claude program directly. |
| Copilot model | Copilot's default | `llm.copilot.model` in `~/.assistant/comms/config.json`, or `COPILOT_MODEL` | The model Copilot sessions and jobs use. `<FEATURE>_COPILOT_MODEL` overrides it for one job. |
| Copilot thinking effort | the shared `llm.droid.reasoning_effort` | `llm.copilot.reasoning_effort` (`off`, `high`, or `max`), or `<FEATURE>_COPILOT_REASONING_EFFORT` | How hard Copilot thinks on background jobs. A model that refuses the setting runs without it. |
| Agent binaries | found on `PATH` | `CLAUDE_BIN`, `COPILOT_BIN`, `DROID_BIN` | Full path to an agent's CLI when it isn't on `PATH`. For an npm Copilot install, background jobs run its standalone build, so they don't need Node. |
| Log size limit | Trim a log past 20 MB to its last 5 MB, after a 24-hour notice | `"logs": {"cap_mb": 20, "keep_mb": 5}` in `~/.assistant/config.json`, alongside your existing keys | Every five minutes the Assistant trims the logs in `~/.assistant/logs`, the `.log` files in `~/.assistant`, `~/.assistant/comms/comms-listen.log`, `~/.claude/logs/workspace-watcher.log`, and `~/.claude/cache/todo-server.log` and `.err`. Both values are in megabytes. A log past `cap_mb` keeps its last `keep_mb` from a line boundary and the older lines are deleted; there is no backup copy. Services keep writing without a restart; a line written during a trim can be lost. **Notice period:** with no `logs` entry, the first time a log is over the limit nothing is trimmed and the dashboard shows a card saying what will be cut and when; trimming starts 24 hours later, with no further notice. A `logs` entry skips the wait. `"cap_mb": 0` turns trimming off. **Add the entry to the file, don't replace it**; for example `{"mode": "pro", "logs": {"cap_mb": 0}}`, then check it with `python3 -m json.tool ~/.assistant/config.json`. If the file exists but can't be read, or a `logs` value isn't a number of 0 or more (a number in quotes counts), trimming stays off and the log says why, once a day. Only a missing file or a missing `logs` key uses the defaults. While a setting can't be read, an amber notice at the top of the dashboard (and a card under **Awaiting input**) says what is wrong and that trimming is off; it goes away once the setting is fixed. If the Assistant can't save its start time in `~/.assistant/log-cap-state.json`, the same bar says trimming is waiting and to check that the folder can be written to. The notice is given once, before trimming first starts; a log that later grows back over the limit is trimmed without a second notice. `keep_mb` is never larger than `cap_mb`. It never touches `.json`, `.jsonl`, or lock files. |
| Local record | On, keeps 90 days, up to 200 MB | `~/.assistant/config.json` → `{"telemetry": {"enabled": true, "keep_days": 90, "max_mb": 200}}` | Assistant keeps a record of what it did, on your Mac only. `enabled` must be `true` to record; anything else turns it off and creates nothing, and so does a `config.json` that can't be read, until you fix it (the pulse log says so). `keep_days` is 1 to 3650 and `max_mb` is 1 to 100000. A setting that can't be read never deletes anything, and the pulse log says so. See [Telemetry](telemetry.md). |

## Observer

The Observer is the model that checks each changed workspace on a pulse and
sorts it: needs you, still working, ready to merge, ready to clean up, or
stuck. It's off by default. While it's off, the pulse sends no workspace to
it, makes no Observer or Observer audit model calls, and each workspace keeps
its last verdict. Everything else the pulse does keeps running: it still
dispatches autoDispatch TODOs, clears stale cards, and updates itself. The
limit of 5 busy sessions still applies, and it is fixed at 5: the pulse counts
busy sessions, including ones you are using, from cmux and each session's
transcript, not from the Observer.

To turn the Observer on, do both of these:

1. Set `{"observer": {"enabled": true}}` in `~/.assistant/config.json`. Only
   the JSON value `true` turns it on. A missing file, unreadable JSON, a
   missing key, or any other value, such as `"true"`, `"on"`, or `1`, leaves it
   off.
2. Run the pulse with `ASSISTANT_WATCHER=1` in its environment.

To turn it off again, set `"enabled"` to `false` or remove the `"observer"`
key. `./bin/assistant-doctor.py` and the dashboard's **Settings and background
services** show whether it's on.

## Background jobs that are off by default

Three Pro mode jobs are off until you turn them on, because nobody read what
they made:

| Job | What it does when on |
|---|---|
| `decisions` | Turns GitHub and session events into a decision list. |
| `brief` | Saves a brief each morning and moves decisions you never saw to the digest. The brief narrator also needs this. |
| `planner` | Suggests the next step for a stalled goal, and records each goal's progress. |

Add the ones you want to `~/.assistant/config.json`, next to `"mode"`:

```json
{"mode": "pro", "jobs": {"decisions": true, "brief": true, "planner": true}}
```

Only the JSON value `true`, written without quotes, turns a job on. A missing
key, any other value, or a file that can't be read leaves it off, and
**Settings and background services** names a value it ignores. There's no
variable for these: the dashboard and the background checks both read this
file, so they always agree. Each job also needs Pro mode, and a change takes
effect at Assistant's next check.

While decisions are off, new events are skipped, not saved for later, so
turning decisions on starts from that moment. Decisions that are already open
still age out: staged ones after 72 hours and digest ones after 24 hours.
Urgent (escalate) ones have no timer, so they stay open until you answer them,
and the Decisions tab keeps listing them under **Awaiting your input**. The
**Completed actions** and **Other updates** lists get no new entries while
decisions are off.

With the planner off, goals stay viewable and editable, but nothing acts on a
goal or records its progress. With the brief off, no brief is saved, and the
Notifications tab shows a live summary instead.

The lesson extractor's pass over repeated successful actions is also off. Set
`ASSISTANT_LESSON_LEDGER_PASS=1` to turn it on. The pass that finds corrections
in your sessions still runs when `ASSISTANT_LESSON_EXTRACTOR=1`.

## Catch-up when you return to a session

When you send a prompt to a Claude session after an hour or more away, this
hook tells Claude to check live state first (git, pull requests, running jobs),
then answer:

- A status question, such as "status?" or "where are we", gets a catch-up of
  six lines or fewer: the goal, what's done, what's left, and the one thing it
  needs from you.
- A one-word decision, such as "yes" or "go", gets one line that says what it
  approves before Claude acts on it.
- Any other prompt gets one line on where the work stands and what changed
  while you were away.

It's off by default. To turn it on, set `ASSISTANT_IDLE_RETURN=1` in
`~/.zprofile` or in the `env` block of `~/.claude/settings.json`, then start
or resume a session. `ASSISTANT_IDLE_RETURN_MINUTES` sets the gap in minutes; the
default is `60`, which is also used when the value isn't a number.

The gap starts at your last prompt, or at Claude's last reply in a turn you
started. In a session you never typed into, such as one Assistant spawned, it
starts at Claude's last reply. Resuming a session, hook output, local slash
commands such as `/model`, API error notices, and turns started by another
session, a background task, a `claude -p` call, or a message automation typed
into the terminal while you were away don't reset it. Automation-typed messages
are recognized by their opening words (for example, "Manager", "Assistant
picked your session", or "This comes from the readiness judge") or by a path under `~/.assistant/spawn-prompts/`. A
message you queue while Claude is busy counts as you being there. None of these
automated prompts counts as your return. `install.sh --apply` registers the hook, `hooks/idle-return.py`, on
`UserPromptSubmit` in `~/.claude/settings.json`. It makes no model calls and
runs in a few hundredths of a second.

## Strategist

The Strategist is the model that drafts context for staged goal steps. Set it
in `~/.assistant/comms/config.json` under `{"strategist": {…}}`:

- `enabled`: default `true`.
- `dailyCostCeilingUsd`: the day's model budget, default `5.0`. When it's hit,
  the Strategist is cut first, never the Observer.

Each goal in `~/.claude/assistant-goals.json` can also set
`budget.maxStrategistCallsPerDay` (default `1`).

With planner autoDispatch off, the Strategist only improves the text of staged
decisions that you review. It never starts unattended work.

## Opt-in daemons

A bare `./install.sh --apply` loads the core set and workspace numbers. Turn on a feature daemon
at the installer's `[y/N]` prompt or with a flag:

| Flag | Turns on |
|---|---|
| `--with-memory` | `memory-sync-pull`: cross-machine lesson and memory sync |
| `--with-crash-resume` | `workspace-watcher`: auto-resumes crashed cmux workspaces |
| `--no-ws-numberer` | Turns off `cmux-ws-numberer`, the `[N]` suffix and color on every workspace. It's on by default. |
| `--with-all` | All of the above |

Slack comms and the Slack reactor are never auto-loaded, even with
`--with-all`. Connectors and `cmux-watcher` also wait until you run
`launchctl load`. [ONBOARDING.md](../ONBOARDING.md) covers how answers are
remembered and how to change your mind.
