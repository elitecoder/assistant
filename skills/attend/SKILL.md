---
name: attend
description: Tell the Assistant to start watching the current cmux workspace again — undoes a prior `/back-off` so Observer runs, verdicts get acted on, and the workspace appears on the dashboard normally. Use when you previously ran `/back-off` here and now want the Assistant's attention back. Mirror skill `/back-off` adds the workspace to the back-off list.
---

# /attend — un-silence the Assistant in this workspace

The user typed `/attend` because they previously ran `/back-off` in this workspace and now want the Assistant looking at it again. **Do not confirm. Do not ask. Execute.**

## Execution

```bash
WS_REF="$(cmux identify 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('caller',{}).get('workspace_ref',''))")"
if [ -z "$WS_REF" ]; then
    echo "ERROR: could not identify caller workspace via cmux identify" >&2
    exit 1
fi
~/dev/assistant/bin/back-off.py remove "$WS_REF"
```

Print the script's stdout verbatim. Possible outputs:

- `removed <ws_ref>` — workspace was on the list, now off it.
- `<ws_ref> not on back-off list` (rc=1) — nothing to do; the Assistant was already attending. Print this verbatim and don't act on it like an error.

## Effect

Takes effect on the Assistant's next pulse (within ~2 minutes). After that, this workspace re-enters the rotation: Observer reads its transcript, emits a verdict, and the Assistant acts on it.
