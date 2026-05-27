---
name: back-off
description: Tell the Assistant to leave the current cmux workspace alone — no Observer calls, no /cleanup or /merge-when-ready sends, no awaiting cards. Use when the Assistant is looping, sending the wrong action, or generally pestering you in a workspace where you'd rather it stayed quiet. Optional reason follows the command. Mirror skill `/attend` removes the workspace from the back-off list.
---

# /back-off — silence the Assistant in this workspace

The user typed `/back-off` because the Assistant is doing something annoying in the current workspace and they want it to stop, immediately. **Do not confirm. Do not ask. Execute.**

## Execution

```bash
WS_REF="$(cmux identify 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('caller',{}).get('workspace_ref',''))")"
if [ -z "$WS_REF" ]; then
    echo "ERROR: could not identify caller workspace via cmux identify" >&2
    exit 1
fi
REASON="${*:-User pressed /back-off}"
~/dev/assistant/bin/back-off.py add "$WS_REF" "$REASON"
```

Pass through any argument the user supplied — that becomes the reason logged on the back-off entry. If they passed nothing, default to a generic message.

Print the script's stdout verbatim. The CLI prints `added <ws_ref>: <reason>`.

## Effect

Takes effect on the Assistant's next pulse (within ~2 minutes). After that, this workspace:

- never has its transcript read by the Observer subagent
- never receives `/cleanup`, `/merge-when-ready`, or any nudge text
- never appears in awaiting cards on the dashboard

To undo, run `/attend` from inside this same workspace, or `~/dev/assistant/bin/back-off.py remove <ws_ref>` from anywhere.

## What this is NOT

- Not a cleanup. The workspace, branch, worktree, dev server, and TODO are all left exactly as they are.
- Not a workspace close. cmux still shows it. You can keep working in it normally.
- Not permanent. The back-off list is a flat JSON file at `~/.assistant/back-off.json`. You manage it; the Assistant only reads it.
