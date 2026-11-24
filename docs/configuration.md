# Configuration

Assistant ships with safe defaults: silent, draft-only, and no unattended goal
work. Every setting below is a deliberate edit you make yourself.

| Setting | Default | Where | What it does |
|---|---|---|---|
| Noise budget | `page:0, notify:0` (fully silent) | `~/.assistant/noise-budget.json` | Maximum pushes a day through the interrupt gate. A `page` also requires lane `escalate`, urgency `now`, and a policy marked `pageable`. |
| Brief wake hour | `7` (local time) | `~/.assistant/comms/config.json` → `{"brief": {"wake_hour": N}}` | The first pulse at or after this hour builds the day's brief. |
| Planner autoDispatch | Off | `~/.assistant/comms/config.json` → `{"planner": {"autoDispatch": true}}` | Off: a stalled goal stages a decision for you to review. On: whitelisted steps dispatch unattended. Turn it on only after you've watched the planner behave. |
| Slack channel ingest | Mentions and DMs only | `SLACK_INGEST_CHANNELS=1` plus a manifest edit | Opts in to full channel message ingestion. See [Connect your sources](connectors.md#slack-read-only). |
| Slack focus list | Not set up | `slack_focus` block in `~/.assistant/comms/config.json` plus `SLACK_USER_TOKEN` | Which channels, bots, repos, and Jira projects the Slack tab uses. See [Slack focus list](connectors.md#slack-focus-list). |
| Connector cadence and caps | 60 s, 200 events, 10 pages | `connectors` block in `~/.assistant/comms/config.json` | Per-connector poll interval and batch caps. The module constants are only the fallback. |
| Coding agent | Claude | `bin/assistant-llm.py set claude\|copilot\|droid\|local [--feature <name>]` | Chooses Claude Code, GitHub Copilot CLI, Factory Droid, or a local model. Without `--feature`, it sets `llm.provider`, which every launch follows: task sessions, the Slack reply session, resumes, `/spawn-claude-workspace`, and the background model jobs (`triage`, `observer`, `observer_audit`, `strategist`, `narrator`, `lessons`, `slack_judge`, `work_queue`). `--feature` picks one job only; `--feature comms` picks the Slack session's agent. With Copilot picked, FFP to-dos still go to Claude. `/spawn-claude-workspace` keeps its own default (`droid`) unless you name an `engine`. If the chosen agent isn't installed, task and Slack sessions start in Claude. `set copilot` also writes cmux's Copilot hooks to `~/.copilot/hooks/cmux.json` so Copilot sessions show on the dashboard and can be restored. Copilot reaches the `/cleanup` and `/merge-when-ready` skills only if `~/.copilot/skills` links to `~/.claude/skills`. `bin/assistant-llm.py status` shows the current routing. |
| Claude alias | `claude` | `bin/assistant-llm.py claude-alias <name>` (saves `llm.claude.alias` in `~/.assistant/comms/config.json`) | The shell alias Claude sessions start with: task sessions, `/spawn-claude-workspace`, and the Slack session. The workspace's own shell runs it, so cmux's Claude wrapper still records the session; if that shell doesn't define the alias, the session starts with `claude` and `assistant-doctor.py` warns. Every restore, by the Assistant or by cmux after a restart or a hibernation wake, replays cmux's saved command, which skips the alias. To keep restored sessions on the alias, declare the alias's script in cmux's [`agents.launchers`](https://cmux.com/docs/configuration#cmux-json) setting; cmux spots the script only when it runs claude as a child process under its own name. The command refuses a name your login zsh doesn't know, a shell keyword such as `if` or `exec`, and `copilot` or `droid`. `claude-alias claude` restores the default. A live Slack session restarts on its next message after the alias changes. The Slack session takes on the alias's own folders and permission mode; the Assistant's model choice still wins. A hand-edited alias that names a shell builtin such as `echo` starts no session, and `assistant-doctor.py` warns. Background model jobs still run the Claude program directly. |
| Copilot model | Copilot's default | `llm.copilot.model` in `~/.assistant/comms/config.json`, or `COPILOT_MODEL` | The model Copilot sessions and jobs use. `<FEATURE>_COPILOT_MODEL` overrides it for one job. |
| Copilot thinking effort | the shared `llm.droid.reasoning_effort` | `llm.copilot.reasoning_effort` (`off`, `high`, or `max`), or `<FEATURE>_COPILOT_REASONING_EFFORT` | How hard Copilot thinks on background jobs. A model that refuses the setting runs without it. |
| Agent binaries | found on `PATH` | `CLAUDE_BIN`, `COPILOT_BIN`, `DROID_BIN` | Full path to an agent's CLI when it isn't on `PATH`. For an npm Copilot install, background jobs run its standalone build, so they don't need Node. |

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
