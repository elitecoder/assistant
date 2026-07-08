# Changelog

All notable changes to the Assistant daemon are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is carried in `pyproject.toml` and `src/assistant/__init__.py`
(`__version__`); keep the two in sync when bumping.

## [0.3.0] - 2026-07-08

### Fixed
- **Lesson proposals now actually reach the operator.** The lesson-extractor
  has always mined recurring patterns and written pending proposals to
  `~/.assistant/proposals.jsonl`, but nothing delivered them: the comms daemon
  suppressed a `lesson-proposal` ledger kind that was never emitted, and the
  warm session (its supposed delivery path) is reactive-only — it wakes on
  inbound Slack and is `/cleared` at 50% context, so it could never proactively
  surface a proposal. The queue sat dead for a month (436 pending proposals
  accumulated, undelivered).

### Added
- **PROPOSALS delivery loop** in `comms-listen.py` (5th daemon loop). Watches
  `proposals.jsonl` as a durable queue and delivers each new pending lesson
  proposal to the comms channel exactly once, asking the operator to confirm.
  - **Exactly-once via an id high-water-mark cursor** (`proposals.cursor`), not
    a byte offset — the confirm path rewrites `proposals.jsonl` in place, which
    would corrupt a byte cursor. Proposal ids are ISO-µs timestamps, so
    `id > cursor` is a clean, rewrite-safe watermark.
  - **Backlog skipped on first run** (like the ledger cursor) so the daemon
    never blasts a stale queue at the operator's phone; recoverable by deleting
    `proposals.cursor` to replay. Deliveries are capped per drain
    (`PROPOSALS_MAX_PER_DRAIN`) so one extractor batch can't firehose.
  - **Halt-and-retry on send failure** — the cursor advances only past a
    successfully-sent proposal, so a failed send retries next pass and no
    proposal is ever silently lost.
  - Each delivery is mirrored into `conversation.jsonl` and carries the
    proposal **id**, so after a `/clear` the warm session can confirm the
    *specific* proposal the operator's `y` refers to (fixing the prior
    `tail -1` bug that confirmed the wrong proposal when several were pending).
  - `comms_lib`: `read_proposals_cursor` / `write_proposals_cursor` /
    `initialize_proposals_cursor_if_missing` / `read_all_proposals` /
    `read_new_proposals` / `fmt_lesson_proposal`.
- Warm-session prompt updated: the daemon delivers proposals (not the warm
  session), and confirmation reads the id back out of the conversation window.

### Notes
- `pattern` and `lesson_audit` proposals are **not** delivered — they have no
  confirm path yet, so pinging them would be a dead-end. Documented gap.

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

[0.2.0]: https://github.com/elitecoder/assistant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/elitecoder/assistant/releases/tag/v0.1.0
