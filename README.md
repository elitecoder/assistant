# Assistant

![platform](https://img.shields.io/badge/platform-macOS-111111)
![python](https://img.shields.io/badge/python-3.11%2B-3572A5)

**Assistant is a chief of staff for your fleet of Claude Code agents.**

It watches your agents, inbox, Slack, GitHub, and calendar, and turns them into
one morning brief and a local dashboard that shows only what needs you.

![The Sessions tab of the Assistant dashboard with sample data: four columns of cmux workspaces. Needs your answer holds a flaky-test fix waiting on a question, Assistant tasks holds a rate-limiting change in progress, Ready to close holds a merged docs rewrite, and Paused holds a backfill you set aside.](docs/images/dashboard-sessions.png)

## Features

### Answer your agents without opening every terminal

When an agent asks you something, its card quotes the question and the choices.
You decide in seconds, then jump straight to the right session.

<img width="520" alt="A Needs your answer card with sample data, opened to the agent's question: keep the test-level retry as a safety net, or remove it now that the race is fixed? Two choices follow, Remove the retry and Keep the retry." src="docs/images/session-question.png">

### Get one ranked brief instead of a dozen inboxes

Plain Python rules sort every notification into escalate, staged, digest, or
drop, and rank what's left. You see the few decisions that matter, and **Wrong
lane** teaches the rules with one tap.

![A decision card from the brief with sample data: "@you can you sanity-check the new rate-limit headers?" marked escalate by the gh-mention-escalate rule, with Accept, Snooze 1h, Reject, and Wrong lane buttons.](docs/images/decision-card.png)

### See the Slack threads where the next move is yours

The **Slack** tab pulls out the threads where someone is waiting on you or
you promised something. You stop scrolling channels to find them.

![The Slack tab with sample data. Do first: reply to Ops about the stuck staging migration, which mentions you. Promise: send the load-test numbers you said you'd share. Each row has an effort tag and a Done button.](docs/images/dashboard-slack.png)

### Also in the box

- Read-only connectors for GitHub, Gmail, Google Calendar, Outlook, and Slack.
  See [Connect your sources](docs/connectors.md).
- Ranked goals with the `/goal` skill, and a task list with the `/todo` skill.
- `/back-off` and `/attend` to tell Assistant to leave a workspace alone or
  watch it again.
- An optional Observer (`ASSISTANT_WATCHER=1`) that writes a status and next
  step for every workspace.
- Silent by default: the notification budget is zero, Assistant never sends mail
  or Slack as you, and it never closes a workspace. See the
  [design reference](docs/design.md).

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
