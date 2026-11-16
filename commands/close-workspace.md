---
description: Close the cmux workspace this session runs in, and nothing else.
allowed-tools: Bash(python3 ~/.claude/commands/close-workspace.py:*)
disable-model-invocation: true
---

!`python3 ~/.claude/commands/close-workspace.py $ARGUMENTS`

The line above is the result of closing this session's cmux workspace, or of `/close-workspace --dry-run`, which names the workspace and closes nothing. The script finds the workspace from the terminal this session runs in, checks it against `CMUX_WORKSPACE_ID`, and closes nothing when it can't be sure.

Tell the user that result in one line, and stop. Don't run any other command that closes, unpins, selects, or looks up a workspace. Never pick a target from `cmux current-workspace`, `cmux identify`, the selected or focused workspace, a sidebar index, or a `[N]` name suffix.
