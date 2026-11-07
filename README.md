# Assistant

![platform](https://img.shields.io/badge/platform-macOS-111111)
![python](https://img.shields.io/badge/python-3.11%2B-3572A5)

**Assistant is a chief of staff for your fleet of Claude Code agents.**

You start a dozen agents, walk away, and come back to a wall of terminals, pull
requests, Slack pings, and email. Assistant watches your agents, inbox, Slack,
GitHub, calendar, and goals, handles what's safe, and hands you one morning
brief with only the decisions you need to make. It runs on your own Mac as a few
small LaunchAgents and a dashboard at `http://127.0.0.1:9876`.

![The Assistant dashboard's Sessions tab, rendered from sample data. Seven cmux workspaces sit in four columns: Needs your answer holds two sessions with open questions, such as "Which transition should the order and profile screens use?"; Assistant tasks holds three sessions in progress with their next step; Ready to close holds a merged docs rewrite; and Paused holds a backfill you set aside. A banner above suggests finishing the oldest session before starting another.](docs/images/dashboard-sessions.png)

Plain Python decides what happens to every item and is the only thing that can
act. Out of the box, Assistant is silent: you read the brief when you're ready.

## Features

### See every agent sorted by what it needs from you

The **Sessions** tab counts your open cmux workspaces and sorts them into
**Needs your answer**, **Assistant tasks**, **Ready to close**, and **Paused**.
When an agent asks you something, the card quotes its question and its choices,
so you can decide before you switch to the session. The dashboard never answers,
closes, or sends instructions for you. See [Use the dashboard](docs/dashboard.md).

<img width="400" alt="A Needs your answer card, rendered from sample data, opened to show the agent's question: &quot;Keep the test-level retry as a safety net, or remove it now that the race is fixed?&quot; with two choices, Remove the retry and Keep the retry, and a note that you answer in your session because the page doesn't send a reply." src="docs/images/session-question.png">

Questions come from your session transcripts without a model call. To also get a
one-line status and next step for every workspace, turn on the Observer with
`ASSISTANT_WATCHER=1`; it's off by default because it reads a lot of transcript
per check.

### Get one morning brief instead of a dozen inboxes

Every GitHub notification, email, calendar reminder, Slack message, and agent
signal becomes one kind of record. Ordered Python rules give each one a lane:
**escalate** goes to the top, **staged** waits for your review, **digest** is an
FYI that expires in 24 hours, and **drop** leaves only a receipt. The first pulse
at or after 7:00 builds the brief.

![The Notifications tab, rendered from sample data. Tiles show 13 notification groups, a trend line of daily alert totals, 1 item handled overnight, 5 FYI digest rows, $0.00 a day, 0 interrupts delivered or denied, and 0 expired unseen. Below, the top groups are a GitHub mention on acme/api #311, an email about a renewal quote, and a workspace waiting for input. Under Completed actions, the rule gmail-newsletter-drop dropped a newsletter.](docs/images/dashboard-notifications.png)

The brief is a pure derivation, so you can rebuild it on demand and see exactly
which rule placed each item and how it scored:

![Terminal output of bin/build-morning-brief.py --print piped through jq, run against sample data. It lists 13 open decisions: five escalations at score 169 from rules such as gh-mention-escalate, gmail-direct-escalate, cmux-needs-input-escalate, slack-dm-escalate, and gh-security-alert-escalate; three staged items at 79, including two review requests and a design review in an hour; and five digest items at 39, such as CI passed on main and Sprint planning.](docs/images/brief-queue.png)

The rules live in
[`config/policies.bootstrap.json`](config/policies.bootstrap.json), and every
rule has a fixture in `evals/policy/`. No starter rule uses an `auto` lane; only
a rule you confirm can. An event that no rule matches opens as an escalation,
and a triage model may then suggest a gentler lane.

### Teach it with one tap

Open a decision to accept, snooze, or reject it. **Wrong lane** rejects it and
files a rule proposal for that source and kind. Repeated identical triage
suggestions and briefs you ignore also become proposals, and nothing becomes a
rule until you confirm it.

![A decision card, rendered from sample data, for "@you can you sanity-check the new rate-limit headers?" It reads escalate, via gh-mention-escalate, score 169, 6.7 hours old, with a TODAY badge, a recommendation to accept today, and Accept, Snooze 1h, Reject, and Wrong lane buttons.](docs/images/decision-card.png)

A brief still unseen after 48 hours moves its open, non-escalated decisions to
the digest instead of nagging you.

### Find the Slack threads where the next move is yours

The **Slack** tab sorts recent threads into **Do first**, **Promise**, **This
week**, and **Optional**. Rules collect and triage the threads, and one model
call writes the titles and sets aside noise. **Done** checks a row off until its
thread gets a new reply. The Slack client refuses any method outside a read
allowlist. See [Slack focus list](docs/connectors.md#slack-focus-list).

![The Slack tab, rendered from sample data with an invented workspace. Do first holds "Reply to Ops about the stuck staging migration," marked as mentioning you; Promise holds "Send the load-test numbers you said you'd share"; This week holds "Weigh in on the API versioning proposal"; and Optional holds "Vote on the team offsite dates." Each row shows an effort tag and a Done button.](docs/images/dashboard-slack.png)

### Connect only the sources you want

Connectors for GitHub, Gmail, Google Calendar, Outlook, and Slack are optional,
read-only daemons. The installer copies them but never loads one, so each
connector starts only when you run `launchctl load`. The **Connected apps** tab
shows each one as **Connected**, **Available**, or **Needs attention**, with the
command to connect it. See [Connect your sources](docs/connectors.md).

![The Connected apps tab, rendered from sample data. GitHub notifications, Gmail, Google Calendar, and Slack show as Connected with their last poll between 1 and 4 minutes ago. Outlook mail shows as Available, with the command to authorize it.](docs/images/dashboard-connections.png)

### Also in the box

- **Goals and a planner.** The `/goal` skill keeps ranked goals. The planner
  stages the next step for a stalled goal as a decision for you, and goals lift
  a decision's rank within its lane.
- **Tasks.** A TODO list on the dashboard and through the `/todo` skill. A task
  flagged to start automatically gets its own cmux workspace, with caps on how
  many run at once.
- **Fleet actions.** With the Observer on, a Python table turns each
  workspace's verdict into a nudge, a `/cleanup`, a narrow merge command, or a
  card for you.
- **`/back-off` and `/attend`.** Tell Assistant to leave a workspace alone, or
  to start watching it again.
- **A noise budget.** `bin/interrupt-gate.py` is the only path to a macOS
  notification, and the default budget is zero pages and zero notifications a
  day. Every denial is recorded in the brief.

## Python owns every gate and every send

Sends are forbidden in config and again in code, so a config edit can't turn
them on. Assistant never sends mail or Slack as you, never closes a workspace,
and never loads a daemon you didn't ask for. Tests fail the build if a
connector contains a send call, if any code closes a workspace, or if anything
other than `bin/interrupt-gate.py` pushes a notification. Automatic merges are
narrow and written for the author's own repository, so expect to adapt
`bin/merge-pr-dispatch.py`. The [design reference](docs/design.md) covers the
details.

## Quick start

To install, run the bootstrap script:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/elitecoder/assistant/main/install-bootstrap.sh)
```

Set `ASSISTANT_REPO_DIR` to clone somewhere other than `~/dev/assistant`, or
`ASSISTANT_REPO_URL` to clone over SSH. Check that it's running, then open
http://127.0.0.1:9876:

```bash
launchctl list | grep com.assistant
~/dev/assistant/bin/assistant-doctor.py
```

To look before you install anything, do a dry run. `install.sh` changes nothing
unless you pass `--apply`:

```bash
git clone https://github.com/elitecoder/assistant.git
cd assistant
./bin/assistant-doctor.py     # read-only preflight
./install.sh                  # prints what it would change
```

A bare `./install.sh --apply` loads the core set: `assistant-pulse`,
`world-scanner`, `session-context-watcher`, `assistant-page`, and
`assistant-todo-server`. Other daemons load only if you opt in. For the full
walkthrough, see [ONBOARDING.md](ONBOARDING.md).

## Requirements

- macOS
- `git`
- Python 3.11 or later
- The [Claude Code CLI](https://claude.ai/code)
- The cmux app, for the workspace features
- [`uv`](https://docs.astral.sh/uv/) (optional; it makes setup faster)

## Learn more

| Topic | Where |
|---|---|
| Install, opt-in features, and troubleshooting | [ONBOARDING.md](ONBOARDING.md) |
| Connect GitHub, Gmail, Google Calendar, Outlook, and Slack | [docs/connectors.md](docs/connectors.md) |
| Using the dashboard tabs and decision cards | [docs/dashboard.md](docs/dashboard.md) |
| Noise budget, wake hour, planner, Strategist, and model settings | [docs/configuration.md](docs/configuration.md) |
| Lanes, ranking, outbound gates, design decisions, and code map | [docs/design.md](docs/design.md) |
| Slack comms design | [docs/assistant-comms-onboarding.md](docs/assistant-comms-onboarding.md) |
| Slack emoji-to-TODO capture | [slack-reactor/README.md](slack-reactor/README.md) |

## Development

The core is standard-library Python; `pyproject.toml` lists no dependencies.
Run the tests:

```bash
uv run --with pytest python -m pytest tests/ -q
```

The Observer eval replays 14 real workspace transcripts against the verdict
prompt. Run it after any change to
[`prompts/observer-batch-prompt.md`](prompts/observer-batch-prompt.md) or
`bin/build-ws-context.py`:

```bash
cd evals/observer && ./run.py
```

Before review, follow the
[new-code quality checks](tests/README.md#check-new-code-before-review). Release
notes live in [CHANGELOG.md](CHANGELOG.md); the current release is 0.7.4.
