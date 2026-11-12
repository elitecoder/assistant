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
| Coding agent | Claude | `bin/assistant-llm.py set claude\|copilot\|droid\|local [--feature <name>]` | Chooses Claude Code, GitHub Copilot CLI, Factory Droid, or a local model. Without `--feature`, it sets `llm.provider`, which every launch follows: task sessions, the Slack reply session, resumes, `/spawn-claude-workspace`, and the background model jobs (`triage`, `observer`, `observer_audit`, `strategist`, `narrator`, `lessons`, `slack_judge`). `--feature` picks one job only. If the chosen agent isn't installed, sessions start in Claude. `bin/assistant-llm.py status` shows the current routing. |
| Copilot model | Copilot's default | `llm.copilot.model` in `~/.assistant/comms/config.json`, or `COPILOT_MODEL` | The model Copilot sessions and jobs use. `<FEATURE>_COPILOT_MODEL` overrides it for one job. |
| Agent binaries | found on `PATH` | `CLAUDE_BIN`, `COPILOT_BIN`, `DROID_BIN` | Full path to an agent's CLI when it isn't on `PATH`. |

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
