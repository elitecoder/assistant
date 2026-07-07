# Changelog

All notable changes to the Assistant daemon are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is carried in `pyproject.toml` and `src/assistant/__init__.py`
(`__version__`); keep the two in sync when bumping.

## [0.4.0] - 2026-07-07

### Added
- **Opt-in daemon tiers.** A bare `install.sh --apply` now installs **CORE only**
  — the fleet loop + dashboard (pulse, world-scanner, session-context-watcher,
  assistant-page, todo-server). Feature daemons are off by default and enabled
  per-flag: `--with-memory` (cross-machine memory sync — only useful on >1
  machine), `--with-crash-resume` (auto-resume crashed cmux workspaces),
  `--with-all`. Slack comms + slack-reactor remain token-gated (copied,
  hand-loaded after setup — never auto-loaded even with `--with-all`). Flags
  only affect a fresh install's loaded set; an already-running feature daemon is
  never torn out, and every feature plist is always copied so it can be enabled
  later. Rationale: a usage analysis showed memory-sync had pulled once ever and
  workspace-watcher fired 3× ever — real features, but not everyone's problem,
  so they shouldn't be forced on. Documented in ONBOARDING.md + install.sh --help.

### Changed
- **assistant-page render cadence 15s → 30s** to match world-scanner's output
  rate — the renderer's only input (world.json) changes every 30s, so rendering
  at 15s made ~half the renders byte-identical no-ops. The dashboard's own 15s
  `<meta>` auto-refresh keeps the browser view as fresh as before. (Not made
  WatchPaths-event-driven: world.json is written non-atomically, so a vnode
  event could fire mid-write — a cadence match is the safe fix.)

### Testing
- `test_install_tiers.py` (5 tests): drives `install.sh` dry-run and asserts
  CORE loads by default, feature daemons are copy-no-load until their `--with`
  flag, and token-gated daemons never auto-load even under `--with-all`.

## [0.3.1] - 2026-07-06

### Fixed
Adversarial ship-gate findings from the 0.3.0 productization (all verified
against the tree before fixing):
- **Symlink write-through guard (R1/R2)** — `install.sh`'s copy path now refuses
  to `cp` through a symlinked target: a legacy install left some
  `~/Library/LaunchAgents/*.plist` as symlinks INTO the repo, so a `--apply`
  would have written rendered `/Users/mukuls` content back through the link into
  the committed portable template. Both `ensure_file_copy` and the Section-3
  loop now replace a symlink with a real rendered file (guard, not follow).
- **Doctor scope accuracy (D1/D2)** — `_history_scopes()` makes the
  conversations.history requirement an EITHER/OR set for EVERY target type,
  closing a false-PASS for a `D…` DM-channel target missing `im:history`; and
  `users:read`/`groups:read` are no longer required (no daemon calls
  users.info / conversations.info). Scope docs across
  ONBOARDING.md / assistant-comms-setup.sh / docs now agree with the doctor.
- **Crash-loop announce dedup (D3/D4)** — `comms-listen`'s misconfig announce
  now dedups on a STABLE key (the set of failing check names, not variable
  error text) and fails CLOSED (writes the marker before sending), so a flapping
  network can't spam the operator every 10s.
- **Portability hardcodes (P1/P2/P3/P5/P6)** — `todo-server.py` (renderer path),
  `comms_session.py` (DISPATCH_CWD + --add-dir), `session-context-watcher.py`
  and `world-scanner.py` (cron cwd), `merge-pr-dispatch.py` (sibling scripts),
  and `render-assistant-page.py` (home shortener) now derive from `__file__` /
  `$HOME` instead of `/Users/mukuls` or a fixed `~/dev/assistant`.
- **Docs vs behavior (D2/D3/D5/D6)** — corrected the false "installer loads
  nothing / loading pulse brings up the always-on set" claims (install.sh
  --apply DOES reload the always-on set; the companion agents are independent),
  the README test/fixture counts (48 / 14), and the install.sh --help COPIED
  list.

### Testing
- `test_plist_portability.py` gains a `bin/*.py` author-literal scan (the
  durable gate against re-introducing `/Users/mukuls` / `~/dev/assistant`
  hardcodes). `test_doctor.py` adds D…-target + users:read-not-required cases.
  Three previously author-pinned tests (renderer, world-scanner, session-context
  cron) now assert the portable behavior via `$HOME`. Full suite green
  (1203 passed, 1 skip).

## [0.3.0] - 2026-07-06

### Added
- **Productization pass** — make Assistant installable + onboardable by any
  macOS engineer with repo access (not just the author):
  - `bin/assistant-doctor.py` — a stdlib preflight that classifies checks
    **core** (python/repo/git/cmux — block install) vs **optional** (Slack
    comms / warm-session — warn only), each with an exact remediation. Wired as
    install.sh phase `[0]`, into `assistant-comms-setup.sh` (refuses to advertise
    the launch line on a scope failure), and into `comms-listen.py` startup
    (announces a misconfig once, deduped, instead of a silent 10s crash-loop).
  - `ONBOARDING.md` — the single "start here" with an ordered activation runbook
    (core pulse → cmux-watcher → Slack comms → slack-reactor → memory); the same
    runbook now prints at the end of `install.sh --apply`.

### Fixed
- **Portable LaunchAgent plists (C1)** — the 10 plists hardcoded `/Users/mukuls`
  and install.sh's `/Users/<user>/` sed was a no-op, so every daemon shipped the
  author's home path and died on any other machine. Plists are now templates
  with `__HOME__`/`__REPO__`/`__PYTHON__`/`__PATH__` tokens that install.sh
  substitutes with arch-resolved values (Apple-Silicon vs Intel interpreter).
  Filenames unchanged so `self_update.py` still keys on them.
- **Dead recovery references (C2)** — the comms warm prompt + `comms_lib.Paths`
  pointed at `heartbeat-write.py`/`spawn-assistant.sh` (deleted in 6bfa86c);
  the "restart Assistant" row now runs the live
  `launchctl kickstart -k … assistant-pulse`.
- **Bootstrap repo URL** — overridable via `ASSISTANT_REPO_URL`; no longer
  implies the author-only `git@github-personal:` SSH alias.
- `slack-reactor` plist downgraded to copy-no-load (it crash-loops without tokens).

### Testing
- 30 new tests: `test_plist_portability.py` (per-plist token render + a true
  fresh-checkout end-to-end that runs `install.sh --apply` from a mukuls-free
  path into a sandboxed HOME, asserting zero `/Users/mukuls` survives — **no
  launchctl**), `test_doctor.py` (core/optional + minimal Slack-scope logic),
  `test_reference_integrity.py` (every referenced script path exists — the
  durable gate against the next 6bfa86c-style orphaning).

## [0.2.0] - 2026-07-06

### Added
- **Slack comms** — a bidirectional comms layer, the Slack re-cut of the
  Telegram/Discord comms removed in 0.1.x for Adobe-IT security reasons. Posts
  to a private channel the bot is invited to (bot token, not the operator's
  identity):
  - Outbound pings: verified/failed ledger actions, cmux-watcher
    workspace-signal pages, and heartbeat-stale pages.
  - Inbound: a warm cmux Claude session (Sonnet) reads every message in the
    channel and replies at top level — a **flat 1:1** model (no threading).
  - `CommsSubsystem` wires into the single-process daemon, additive: active only
    when Slack is fully configured (`has_slack`).
  - Transport CLIs (`slack-send.py`, `slack-poll.py`), shared `comms_lib`, warm
    `comms_session`, the `comms-listen.py` daemon, `assistant-comms-setup.sh`,
    the opt-in LaunchAgent, and onboarding docs.
- **Send-gate** — `slack-send.py` and `src/assistant/slack.send()` refuse, with
  no API call, any target not in `config.slack.allowed_targets` (seeded to the
  one comms channel). Defense-in-depth confining the bot to its channel.
- Inbox freshness gate: stale cmux-watcher signals (older than
  `COMMS_INBOX_MAX_AGE_SEC`, default 300s) are dropped without a ping.

### Changed
- Daemon self-cleanup of its own warm session restored under a title guard
  (`close_own_workspace` only ever closes an `assistant-comms (warm)`
  workspace); `test_no_close_workspace.py` narrowed to allowlist exactly that
  one guarded invocation while still banning every other close-workspace call.

## [0.1.0] - 2026-06-06

### Added
- Initial single-process daemon (`python -m assistant`): collapses the pulse
  timer + heartbeat into one binary with a subsystem-per-thread model
  (`pulse`, `tools`, `heartbeat`).

### Removed
- Telegram and Discord comms transports (later in the 0.1.x line), removed
  entirely for Adobe-IT security reasons — the direct predecessor of the 0.2.0
  Slack comms above.

[0.4.0]: https://github.com/elitecoder/assistant/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/elitecoder/assistant/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/elitecoder/assistant/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/elitecoder/assistant/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/elitecoder/assistant/releases/tag/v0.1.0
