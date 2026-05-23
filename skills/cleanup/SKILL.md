---
name: cleanup
description: Tear down the current workspace — kill dev servers, stash uncommitted changes, delete branch, remove worktree, mark related TODO done, close cmux workspace. Use when user types /cleanup, says "clean up this workspace", "I'm done here", "tear this down", or "wrap up". The user typed the command — DO NOT confirm, DO NOT ask, just execute. Writes a ledger entry so /cleanup --undo can recover.
---

# /cleanup — workspace teardown

The user typed `/cleanup` for a reason. **Do not confirm. Do not ask. Execute.**

The user is inside a cmux workspace they want to dismiss. This skill kills the dev servers, discards uncommitted changes (with a stash safety net), deletes the branch and worktree, marks the linked TODO done, and closes the cmux workspace. One ledger entry captures the whole operation for `/cleanup --undo`.

## Flags

- (none) — full default cleanup (stash · delete branch · remove worktree · mark TODO done · close ws)
- `--keep-branch` — skip `git branch -D`
- `--keep-worktree` — skip `git worktree remove`
- `--keep-todo` — skip the TODO status flip
- `--keep-ws` — skip `cmux close-workspace`
- `--discard` — use `git reset --hard` instead of `git stash` (truly throws away — required for the ABSOLUTE RULE on destructive actions)
- `--dry-run` — print what would happen, write nothing, kill nothing
- `--undo <ledger-id>` — reverse a previous cleanup (recreate worktree, pop stash, mark TODO open, **note: cmux workspace closure is irreversible**)

## Execution

### Step 1 — capture state (no side effects yet)

```bash
set -u
TS=$(date +%Y%m%d-%H%M%S)
LEDGER_ID="cleanup-$TS"
LEDGER_DIR="$HOME/.architect/orchestrator-ledger"
mkdir -p "$LEDGER_DIR"
LEDGER="$LEDGER_DIR/$LEDGER_ID.json"

# Workspace + cwd
ORIGIN=$(cmux identify --json)
WS_REF=$(printf '%s' "$ORIGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["caller"]["workspace_ref"])')
CWD_REAL=$(pwd -P)

# Branch + worktree
BRANCH=$(git -C "$CWD_REAL" branch --show-current 2>/dev/null || true)
GIT_TOPLEVEL=$(git -C "$CWD_REAL" rev-parse --show-toplevel 2>/dev/null || true)
GIT_COMMON=$(git -C "$CWD_REAL" rev-parse --git-common-dir 2>/dev/null | xargs -I {} python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" {} 2>/dev/null || true)
IS_WORKTREE=0
if [ -n "$GIT_TOPLEVEL" ] && [ -n "$GIT_COMMON" ]; then
  # If common dir is not directly under toplevel/.git, we're in a linked worktree
  if [ "$GIT_COMMON" != "$GIT_TOPLEVEL/.git" ]; then
    IS_WORKTREE=1
  fi
fi

# Uncommitted changes count
UNCOMMITTED=$(git -C "$CWD_REAL" status --porcelain 2>/dev/null | wc -l | tr -d ' ')

# Dev server pids in this cwd subtree
DEV_PIDS=$(pgrep -laf -- "$CWD_REAL" | grep -E "(vite|storybook|tsc -w|next dev|webpack|esbuild|jest --watch)" | awk '{print $1}' | tr '\n' ' ')

# Related TODO — title-fuzzy-match against workspace title and current branch
WS_TITLE=$(cmux tree --workspace "$WS_REF" --json 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
for win in d.get('windows', []):
    for ws in win.get('workspaces', []):
        if ws.get('ref') == sys.argv[1]:
            print(ws.get('title') or '')
            sys.exit(0)
" "$WS_REF")

RELATED_TID=$(python3 <<PYEOF
import json, os, re
todo = json.load(open(os.path.expanduser("~/.claude/assistant-todo.json")))
title_low = "$WS_TITLE".lower()
branch_low = "$BRANCH".lower()
def keywords(s):
    return set(w for w in re.findall(r"[a-z0-9]{4,}", s.lower()) if w not in {"auto","fix","squirrel","ffp","claude"})
ws_kw = keywords(title_low + " " + branch_low)
best = (None, 0)
for it in todo.get("items", []):
    if it.get("status") in ("done","deferred"): continue
    # Direct dispatchedWs match wins
    if it.get("dispatchedWs") == "$WS_REF":
        print(it["id"]); exit()
    it_kw = keywords(it.get("title","") + " " + (it.get("detail") or ""))
    if not it_kw or not ws_kw: continue
    overlap = len(it_kw & ws_kw) / min(len(it_kw), len(ws_kw))
    if overlap > best[1]:
        best = (it["id"], overlap)
if best[1] >= 0.5:
    print(best[0])
PYEOF
)
```

### Step 2 — log the planned action to the ledger

Write a JSON file describing what's about to happen. Even on `--dry-run`, the ledger entry is the record.

```bash
python3 <<PYEOF
import json, os
from datetime import datetime, timezone
ledger = {
    "id": "$LEDGER_ID",
    "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "action": "cleanup",
    "workspace_ref": "$WS_REF",
    "workspace_title": "$WS_TITLE",
    "cwd": "$CWD_REAL",
    "branch": "$BRANCH" or None,
    "is_worktree": bool($IS_WORKTREE),
    "uncommitted_lines": $UNCOMMITTED,
    "dev_pids": "$DEV_PIDS".split(),
    "related_todo": "$RELATED_TID" or None,
    "flags": {
        "keep_branch": $KEEP_BRANCH,
        "keep_worktree": $KEEP_WORKTREE,
        "keep_todo": $KEEP_TODO,
        "keep_ws": $KEEP_WS,
        "discard": $DISCARD,
        "dry_run": $DRY_RUN,
    },
    "stash_ref": None,
    "steps": [],
    "undone": False,
}
open("$LEDGER", "w").write(json.dumps(ledger, indent=2))
PYEOF
```

If `--dry-run`, print the ledger and stop here.

### Step 3 — kill dev servers

```bash
if [ -n "$DEV_PIDS" ]; then
  for pid in $DEV_PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  # Clean up survivors
  for pid in $DEV_PIDS; do
    kill -9 "$pid" 2>/dev/null || true
  done
fi
# Append to ledger.steps
python3 -c "
import json
d = json.load(open('$LEDGER'))
d['steps'].append({'kind':'kill-dev-servers','pids':'$DEV_PIDS'.split(),'ok':True})
json.dump(d, open('$LEDGER','w'), indent=2)
"
```

### Step 4 — handle uncommitted changes

If `$UNCOMMITTED > 0`:

**Default (stash safety net):**
```bash
STASH_MSG="auto-cleanup-$LEDGER_ID"
if git -C "$CWD_REAL" stash push -u -m "$STASH_MSG" 2>/dev/null; then
  STASH_REF=$(git -C "$CWD_REAL" stash list | grep -m1 "$STASH_MSG" | awk -F: '{print $1}')
  python3 -c "
import json
d = json.load(open('$LEDGER'))
d['stash_ref'] = '$STASH_REF'
d['steps'].append({'kind':'stash','msg':'$STASH_MSG','ref':'$STASH_REF','ok':True})
json.dump(d, open('$LEDGER','w'), indent=2)
"
fi
```

**With `--discard`:**
```bash
git -C "$CWD_REAL" reset --hard HEAD 2>/dev/null
git -C "$CWD_REAL" clean -fd 2>/dev/null
python3 -c "
import json
d = json.load(open('$LEDGER'))
d['steps'].append({'kind':'discard-hard-reset','ok':True,'recovery':'NOT_RECOVERABLE'})
json.dump(d, open('$LEDGER','w'), indent=2)
"
```

### Step 5 — leave the worktree (if we're inside one) BEFORE removing it

```bash
ORIGIN_REPO=""
if [ "$IS_WORKTREE" = "1" ] && [ "$KEEP_WORKTREE" = "0" ]; then
  ORIGIN_REPO=$(git -C "$CWD_REAL" rev-parse --git-common-dir | xargs -I {} python3 -c "import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))" {})
  cd "$ORIGIN_REPO"
fi
```

### Step 6 — delete branch (unless --keep-branch)

```bash
if [ "$KEEP_BRANCH" = "0" ] && [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ]; then
  git -C "$ORIGIN_REPO" branch -D "$BRANCH" 2>/dev/null && OK=1 || OK=0
  python3 -c "
import json
d = json.load(open('$LEDGER'))
d['steps'].append({'kind':'delete-branch','branch':'$BRANCH','ok':bool($OK)})
json.dump(d, open('$LEDGER','w'), indent=2)
"
fi
```

### Step 7 — remove worktree (unless --keep-worktree)

```bash
if [ "$IS_WORKTREE" = "1" ] && [ "$KEEP_WORKTREE" = "0" ]; then
  git -C "$ORIGIN_REPO" worktree remove --force "$CWD_REAL" 2>/dev/null && OK=1 || OK=0
  python3 -c "
import json
d = json.load(open('$LEDGER'))
d['steps'].append({'kind':'remove-worktree','path':'$CWD_REAL','ok':bool($OK)})
json.dump(d, open('$LEDGER','w'), indent=2)
"
fi
```

### Step 8 — mark TODO done (unless --keep-todo)

If `$RELATED_TID` is non-empty and `--keep-todo` is off, invoke the `/todo` skill subcommand internally:

```bash
if [ "$KEEP_TODO" = "0" ] && [ -n "$RELATED_TID" ]; then
  # Apply the same atomic JSON mutation /todo done does
  python3 <<PYEOF
import json, os
from datetime import datetime, timezone
PATH = os.path.expanduser("~/.claude/assistant-todo.json")
data = json.load(open(PATH))
for it in data.get("items", []):
    if it.get("id") == "$RELATED_TID":
        it["status"] = "done"
        it["statusUpdatedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
        it["doneBy"] = "cleanup:$LEDGER_ID"
        break
data["_lastUpdated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
tmp = PATH + ".tmp"
open(tmp, "w").write(json.dumps(data, indent=2))
os.replace(tmp, PATH)
PYEOF
  python3 -c "
import json
d = json.load(open('$LEDGER'))
d['steps'].append({'kind':'todo-done','tid':'$RELATED_TID','ok':True})
json.dump(d, open('$LEDGER','w'), indent=2)
"
fi
```

### Step 9 — close cmux workspace (unless --keep-ws)

This is the only **irreversible** step. Do it last.

```bash
if [ "$KEEP_WS" = "0" ]; then
  cmux close-workspace --workspace "$WS_REF" 2>/dev/null && OK=1 || OK=0
  python3 -c "
import json
d = json.load(open('$LEDGER'))
d['steps'].append({'kind':'close-workspace','ws':'$WS_REF','ok':bool($OK),'irreversible':True})
json.dump(d, open('$LEDGER','w'), indent=2)
"
fi
```

### Step 10 — nudge Triage and report

```bash
~/.claude/bin/triage-pulse.sh 2>/dev/null || true

# Single-line summary
echo "✓ cleanup $LEDGER_ID — ws=$WS_REF branch=${BRANCH:-none} worktree=$([ "$IS_WORKTREE" = "1" ] && echo "removed" || echo "n/a") todo=${RELATED_TID:-none} stash=${STASH_REF:-none}"
echo "  undo: /cleanup --undo $LEDGER_ID"
```

## /cleanup --undo <ledger-id>

Walk the ledger steps in reverse. The order:

1. **Reopen TODO** — flip `status` back to `open` (or whatever it was; ledger could record prior status if you care)
2. **Recreate worktree** — `git worktree add <path> <branch>` (only works if branch still exists or you preserved it)
3. **Restore branch** — only possible if recently deleted (use reflog: `git reflog show <branch>` if entries still exist)
4. **Pop the stash** — `git stash pop <stash_ref>`
5. **Mark ledger as `undone: true`**

**Cannot undo:**
- The cmux workspace closure (cmux gives the next workspace a new ref number)
- `--discard`'d changes (no stash exists)
- Any step where `recovery: NOT_RECOVERABLE` was written

The undo path prints exactly which steps it could and could not reverse, with file/branch evidence.

## Guardrails

- **No confirmation prompts. Ever.** The user typed `/cleanup`. Asking is the bug.
- **`--discard` requires the explicit flag** because of the ABSOLUTE RULE on destructive ops. Without it, `git stash` is the rope.
- **Refuse to delete `main` or `master` branch** — guardrail in step 6.
- **`cwd` must be inside `~/dev/**` or `~/.claude/worktrees/**`** — refuse otherwise to avoid running cleanup outside intended scope.
- **One ledger entry per cleanup** — even partial cleanups (some steps fail) write a complete ledger so undo knows what was done.
- **No subprocess on the cmux origin** — never run `cmux close-workspace` against a workspace that isn't the one the user invoked from. `WS_REF` comes from `cmux identify --json` (caller), never from arguments.
- **Idempotent on failure** — each step's `ok: bool` is recorded. Re-running `/cleanup` after a partial failure picks up where the last one left off (steps with `ok:true` skip).

## Examples

```
/cleanup
→ ✓ cleanup cleanup-20260522-211530 — ws=workspace:117 branch=ffp/multi-select-speed-bugs
   worktree=removed todo=td-024 stash=stash@{0}
   undo: /cleanup --undo cleanup-20260522-211530

/cleanup --keep-branch
→ ✓ cleanup cleanup-... — ws=workspace:118 branch=preserved worktree=removed todo=td-025 stash=stash@{0}

/cleanup --discard
→ ✓ cleanup cleanup-... — uncommitted changes RESET (NOT recoverable). ws closed.

/cleanup --dry-run
→ would: kill 3 dev pids · stash 12 uncommitted lines · delete branch ffp/foo · remove worktree at ~/dev/.../foo
        · mark td-031 done · close workspace:120
   ledger written but no side effects.

/cleanup --undo cleanup-20260522-211530
→ ✓ undone cleanup-20260522-211530 — popped stash · recreated worktree · marked td-024 open
   ⚠ workspace:117 cannot be reopened (cmux closure is irreversible — open a new workspace if you need to resume)
```
