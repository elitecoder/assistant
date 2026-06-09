---
paths:
  - "**/*"
---

# Assistant Lessons

<!-- Rules below are managed by `assistant-curator.py write --target ...`. Keep the `## Lessons` heading — the curator uses it as the insert anchor. -->

## Lessons

Project-scoped rules for Assistant, managed by `assistant-curator.py write --target ...`. Each block is a rule that travels with this repo and applies to every session that reads a file matching this file's `paths:` glob.

<!-- lesson: sending-a-morning-status-update, scope: comms, added: 2026-06-06 -->
**Sending a morning status update or daily briefing to the user**

Lead every status update with the work itself — what's happening, what's next, what's blocked — and push workspace references, IDs, and technical metadata to the end or secondary position. The user's attention is on outcomes and context, not infrastructure labels.

<!-- lesson: about-to-run-a-self, scope: daemon, added: 2026-06-06 -->
**About to run a self-update (git pull) on any repo**

Check the working tree first (git status --porcelain). Local commits ahead of the remote ALWAYS block the pull — surface them, never stash or discard. A dirty working tree also defers the pull and is surfaced — UNLESS it has stayed continuously dirty past the configured window (default 1 day, clocked from the first pulse that observed it dirty), in which case auto-stash it (git stash push -u, always recoverable via git stash pop) and proceed. Never reset/clean/force; the operator's work is only ever parked in a recoverable stash, never lost.

