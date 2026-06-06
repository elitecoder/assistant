---
paths:
  - "**/*"
---

# Assistant Lessons

<!-- Rules below are managed by `assistant-curator.py write --target ...`. Keep the `## Lessons` heading — the curator uses it as the insert anchor. -->

## Lessons

Project-scoped rules for Assistant, managed by `assistant-curator.py write --target ...`. Each block is a rule that travels with this repo and applies to every session that reads a file matching this file's `paths:` glob.

<!-- lesson: about-to-run-a-self, scope: daemon, added: 2026-06-06 -->
**About to run a self-update (git pull) on any repo**

Before pulling, check if the working tree is dirty (`git status --porcelain`). If there are uncommitted changes or untracked files that would be overwritten, skip the pull and surface the reason — never silently defer without telling the user. Only proceed with the pull when the tree is clean or the user explicitly approves a stash-then-pull.

