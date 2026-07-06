# Onboarding Assistant

This is the single "start here" for getting Assistant running on a macOS machine
you have repo access to. The installer copies code and LaunchAgent plists but
**loads nothing** — activation is opt-in and ordered. Follow the phases below;
each is independent, so stop after CORE if that's all you want.

Run `./bin/assistant-doctor.py` at any point for a preflight health check.

---

## 0. Prerequisites

| Requirement | Why | Check / install |
|---|---|---|
| macOS (Apple-Silicon or Intel) | LaunchAgents | — |
| `git`, `python3` ≥ 3.11 | daemons + installer | `python3 --version` |
| [`claude` CLI](https://claude.ai/code) | pulse dispatch + warm comms session | `command -v claude` |
| `cmux.app` | Assistant drives Claude in cmux workspaces | install cmux.app, or set `CMUX_BIN` |
| repo access | clone the private repo | your GitHub SSH key |
| `uv` (optional) | faster venvs | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

The doctor classifies checks **core** (block a working install) vs **optional**
(only needed for a specific feature). A pulse-only install needs only the core row.

## 1. Install

```bash
ASSISTANT_REPO_URL=git@github.com:elitecoder/assistant.git \
  bash <(curl -fsSL https://raw.githubusercontent.com/elitecoder/assistant/main/install-bootstrap.sh)
```

`ASSISTANT_REPO_URL` is overridable because the repo is private — clone over the
SSH identity you actually have. (Don't use the author's `git@github-personal:`
alias; it resolves only on the author's machine.) The bootstrap clones to
`~/dev/assistant`, runs the preflight, and runs `install.sh --apply`. `install.sh`
is idempotent and dry-run by default; re-run it freely.

---

## Activation runbook (ordered — the installer prints this too)

### CORE — the pulse orchestrator (start here)

```bash
launchctl load ~/Library/LaunchAgents/com.assistant.assistant-pulse.plist
```

This is the mechanical orchestrator: every ~5 min it observes your cmux
workspaces and acts on what's safe (merge, cleanup, nudge). Loading the pulse
agent also brings up the always-on set — the dashboard page renderer, the todo
server, the session-context watcher, the workspace watcher, and the world
scanner. Verify:

```bash
launchctl list | grep com.assistant
stat -f '%Sm' ~/.claude/cache/world.json   # should refresh within ~30s
```

### OPTIONAL — workspace-signal pings

Only needed if you want the "a workspace needs your input / finished" pings
(including through Slack comms). Loads the cmux event watcher:

```bash
launchctl load ~/Library/LaunchAgents/com.mukul.assistant-cmux-watcher.plist
```

### OPTIONAL — Slack comms (bidirectional)

A private Slack channel becomes a 1:1 line to this machine: it pings you about
actions + workspace signals + heartbeat staleness, and a warm Claude session
answers your messages. Requires the cmux-watcher above for workspace pings.

1. Create a **private** Slack channel (e.g. `#assistant-comms`) and `/invite` the bot.
2. Add bot scopes at api.slack.com/apps → OAuth → Bot Token Scopes:
   `chat:write`, `groups:history`, `users:read` (add `im:write`, `im:history`
   if you target a DM instead of a channel). Reinstall the app if scopes changed.
3. `export SLACK_BOT_TOKEN=xoxb-…` in `~/.zprofile` (optionally
   `export SLACK_PING_TARGET=C…` = your channel id).
4. Run setup — it validates the token, writes `~/.assistant/config.json` (chmod
   600, token never stored), sends a test message, and runs a **preflight** that
   refuses to print the launch line if a scope/binary/auth check fails:

   ```bash
   ./bin/assistant-comms-setup.sh
   ```

5. When the preflight is green, load the daemon (it prints the exact line):

   ```bash
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.assistant.assistant-comms.plist
   # stop with: launchctl bootout gui/$UID/com.assistant.assistant-comms
   ```

The bot is confined to that one channel by a send-gate (`config.slack.allowed_targets`)
enforced with no API call — it can post nowhere else. See
[docs/assistant-comms-onboarding.md](docs/assistant-comms-onboarding.md) for the full design.

### OPTIONAL — Slack emoji → todo capture

React with your machine's emoji on any Slack thread to capture it as a `/todo`.
Needs `SLACK_APP_TOKEN` + `SLACK_BOT_TOKEN` in `~/.zprofile`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.assistant.slack-reactor.plist
```

See [slack-reactor/README.md](slack-reactor/README.md).

### OPTIONAL — cross-machine memory

The installer's memory step (interactive) offers: (1) owner sync from a private
memory repo, or (2) local-only semantic memory at `~/.assistant/mem0/`. Pick (2)
unless you are the owner with that repo's SSH access.

---

## Troubleshooting

- **Nothing happens after loading pulse** → `./bin/assistant-doctor.py` (core
  checks); tail `~/.assistant/logs/assistant-pulse.launchd.err`.
- **Comms pings but never replies** → almost always a missing Slack scope or the
  warm-session `claude` binary. `./bin/assistant-doctor.py --only slack` names it.
- **A daemon won't launch** → check the plist rendered correctly:
  `grep -o '__[A-Z]*__' ~/Library/LaunchAgents/com.assistant.*.plist` should
  return nothing (any surviving `__TOKEN__` means the installer didn't substitute).
- **Re-run the installer** anytime — it's idempotent and backs up anything it replaces.
