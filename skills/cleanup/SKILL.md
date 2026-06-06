---
name: cleanup
description: Tear down the current workspace — kill dev servers, stash uncommitted changes, delete branch, remove worktree, mark related TODO done. Use when user types /cleanup, says "clean up this workspace", "I'm done here", "tear this down", or "wrap up". The user typed the command — DO NOT confirm, DO NOT ask, just execute. Does NOT close the cmux workspace — the user closes it manually after confirming the work shipped. Writes a ledger entry so /cleanup --undo can recover.
---

# /cleanup — workspace teardown

The user typed `/cleanup` for a reason. **Do not confirm. Do not ask. Execute.**

The user is inside a cmux workspace they want to dismiss. This skill kills the dev servers, discards uncommitted changes (with a stash safety net), deletes the branch and worktree, and marks the linked TODO done (or synthesizes a done TODO if none matched, so the list stays a complete ledger of shipped work). One ledger entry captures the whole operation for `/cleanup --undo`.

**It does NOT close the cmux workspace.** That capability was removed 2026-05-26 after work-loss incidents — with the workspace gone, the user could no longer tell whether their session had finished cleanly or been interrupted. The user now closes the workspace manually once they have confirmed the work shipped. (Re-introduced and re-removed 2026-06-05 — see Step 9 below for why it stays out.)

## Flags

- (none) — full default cleanup (stash · delete branch · remove worktree · mark TODO done)
- `--keep-branch` — skip `git branch -D`
- `--keep-worktree` — skip `git worktree remove`
- `--keep-todo` — skip the TODO status flip
- `--keep-ws` — accepted for backward compat but a no-op (workspace closure is always skipped now)
- `--discard` — use `git reset --hard` instead of `git stash` (truly throws away — required for the ABSOLUTE RULE on destructive actions)
- `--dry-run` — print what would happen, write nothing, kill nothing
- `--undo <ledger-id>` — reverse a previous cleanup (recreate worktree, pop stash, mark TODO open)

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

# Related TODO — DETERMINISTIC resolution only. No fuzzy / semantic matching.
# dispatch_todo (bin/pulse.py) stamps the TODO with dispatchedWs=<ws_ref> AND
# names the workspace "td-NNN: <title>", so cleanup has two exact signals.
WS_TITLE=$(cmux tree --workspace "$WS_REF" --json 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
for win in d.get('windows', []):
    for ws in win.get('workspaces', []):
        if ws.get('ref') == sys.argv[1]:
            print(ws.get('title') or '')
            sys.exit(0)
" "$WS_REF")

# Signal 1 (preferred): a TODO whose dispatchedWs == this workspace ref.
# Signal 2 (fallback): an exact td-NNN id parsed from the workspace title.
# Both are exact lookups — if neither hits, RELATED_TID is empty and Step 8
# synthesizes a done item. No scoring, no thresholds, no guessing.
RELATED_TID=$(python3 <<PYEOF
import json, os, re
todo = json.load(open(os.path.expanduser("~/.claude/assistant-todo.json")))
items = todo.get("items", [])

# Signal 1: exact dispatchedWs match.
for it in items:
    if it.get("dispatchedWs") == "$WS_REF":
        print(it["id"]); raise SystemExit

# Signal 2: exact td-NNN from the workspace title, verified to exist as an item.
m = re.search(r"\b(td-\d{3,4})\b", "$WS_TITLE")
if m:
    tid = m.group(1)
    if any(it.get("id") == tid for it in items):
        print(tid)
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

### Step 8 — mark TODO done, or synthesize one (unless --keep-todo)

If `--keep-todo` is off:
- **Matched** (`$RELATED_TID` non-empty) → flip that item's status to `done`.
- **No match** → synthesize a new `done` item from the workspace so the TODO
  list stays a complete ledger of shipped work. The synthesized item is born
  `done` with `autoDispatch:false` (never re-dispatched) and `dispatchedWs`
  set to this workspace, and the ledger records a `todo-synthesized` step so
  `--undo` can remove it.

```bash
if [ "$KEEP_TODO" = "0" ]; then
  python3 <<PYEOF
import json, os
from datetime import datetime, timezone
PATH = os.path.expanduser("~/.claude/assistant-todo.json")
LEDGER = "$LEDGER"
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
data = json.load(open(PATH))

ledger = json.load(open(LEDGER))
tid = "$RELATED_TID"

if tid:
    # Matched an existing TODO — flip it done (same mutation /todo done does).
    for it in data.get("items", []):
        if it.get("id") == tid:
            it["status"] = "done"
            it["statusUpdatedAt"] = now
            it["doneBy"] = "cleanup:$LEDGER_ID"
            break
    ledger["steps"].append({"kind": "todo-done", "tid": tid, "ok": True})
else:
    # No match — synthesize a done item so we have a record of this work.
    used = set()
    for bucket in ("items", "completed", "removed"):
        for it in data.get(bucket, []) or []:
            try: used.add(int((it.get("id") or "").lstrip("td-")))
            except ValueError: pass
    tid = "td-%03d" % ((max(used) + 1) if used else 1)
    title = ("$WS_TITLE".strip() or "$BRANCH".strip()
             or "work in $WS_REF")[:120]
    item = {
        "id": tid,
        "priority": "P3",
        "title": title,
        "source": "cleanup-synthesized:$WS_REF",
        "createdAt": now,
        "status": "done",
        "statusUpdatedAt": now,
        "completedAt": now,
        "doneBy": "cleanup:$LEDGER_ID",
        "autoDispatch": False,
        "dispatchedWs": "$WS_REF",
        "detail": ("Reconstructed at cleanup — no matching TODO existed. "
                   "branch=$BRANCH cwd=$CWD_REAL"),
    }
    data.setdefault("items", []).append(item)
    ledger["steps"].append({"kind": "todo-synthesized", "tid": tid, "ok": True})

data["_lastUpdated"] = now
tmp = PATH + ".tmp"
open(tmp, "w").write(json.dumps(data, indent=2))
os.replace(tmp, PATH)
json.dump(ledger, open(LEDGER, "w"), indent=2)
print("RELATED_TID=" + tid)
PYEOF
fi
```

The synthesized id is echoed as `RELATED_TID=td-NNN`; capture it if you want the Step 10 summary to show the real id rather than `none`.

### Step 9 — do NOT close the cmux workspace

**`/cleanup` never closes the cmux workspace.** Stop after the teardown above
and leave the workspace open. Removed 2026-05-26 after work-loss incidents:
once the workspace was gone, the user could not tell a clean finish from an
interrupted one, and an auto-fired `/cleanup` on a misclassified session
destroyed live work. The user closes the workspace manually — `/close-workspace`
or the cmux UI — once they have confirmed the work shipped.

Do not run `cmux close-workspace` here under any circumstances, including when
the caller is the orchestrator. If a workspace genuinely needs to be closed
programmatically, that is the Observer's separate `close-workspace` action, not
this skill.

### Step 10 — report

The mechanical orchestrator (`bin/pulse.py`) picks up the cleaned-up workspace on its next 5-min pulse; the dashboard re-renders within 15s. No nudge call is needed.

```bash
# Single-line summary
echo "✓ cleanup $LEDGER_ID — ws=$WS_REF branch=${BRANCH:-none} worktree=$([ "$IS_WORKTREE" = "1" ] && echo "removed" || echo "n/a") todo=${RELATED_TID:-none} stash=${STASH_REF:-none}"
echo "  undo: /cleanup --undo $LEDGER_ID"
```

## /cleanup --undo <ledger-id>

Walk the ledger steps in reverse. The order:

1. **Reopen or remove TODO** —
   - `todo-done` step → flip `status` back to `open` (or whatever it was; ledger could record prior status if you care).
   - `todo-synthesized` step → the item was created by this cleanup and never existed before, so **remove it** (soft-move to `removed[]`, matching `/todo rm`). Reopening it would leave a phantom open TODO for work that's already shipped.
2. **Recreate worktree** — `git worktree add <path> <branch>` (only works if branch still exists or you preserved it)
3. **Restore branch** — only possible if recently deleted (use reflog: `git reflog show <branch>` if entries still exist)
4. **Pop the stash** — `git stash pop <stash_ref>`
5. **Mark ledger as `undone: true`**

**Cannot undo:**
- `--discard`'d changes (no stash exists)
- Any step where `recovery: NOT_RECOVERABLE` was written

The undo path prints exactly which steps it could and could not reverse, with file/branch evidence.

## Guardrails

- **No confirmation prompts. Ever.** The user typed `/cleanup`. Asking is the bug.
- **`--discard` requires the explicit flag** because of the ABSOLUTE RULE on destructive ops. Without it, `git stash` is the rope.
- **Refuse to delete `main` or `master` branch** — guardrail in step 6.
- **`cwd` must be inside `~/dev/**` or `~/.claude/worktrees/**`** — refuse otherwise to avoid running cleanup outside intended scope.
- **One ledger entry per cleanup** — even partial cleanups (some steps fail) write a complete ledger so undo knows what was done.
- **Never closes the cmux workspace** — `/cleanup` performs the teardown only; the user closes the workspace by hand after confirming the work shipped (see Step 9). This skill must never run `cmux close-workspace`.
- **Idempotent on failure** — each step's `ok: bool` is recorded. Re-running `/cleanup` after a partial failure picks up where the last one left off (steps with `ok:true` skip).

## Examples

```
/cleanup
→ ✓ cleanup cleanup-20260522-211530 — ws=workspace:117 branch=ffp/multi-select-speed-bugs
   worktree=removed todo=td-024 stash=stash@{0}
   workspace left open — close it yourself once you've confirmed the work shipped
   undo: /cleanup --undo cleanup-20260522-211530

/cleanup --keep-branch
→ ✓ cleanup cleanup-... — ws=workspace:118 branch=preserved worktree=removed todo=td-025 stash=stash@{0}

/cleanup --discard
→ ✓ cleanup cleanup-... — uncommitted changes RESET (NOT recoverable). workspace left open.

/cleanup --dry-run
→ would: kill 3 dev pids · stash 12 uncommitted lines · delete branch ffp/foo · remove worktree at ~/dev/.../foo
        · mark td-031 done  (workspace:120 left open — not closed)
   ledger written but no side effects.

/cleanup --undo cleanup-20260522-211530
→ ✓ undone cleanup-20260522-211530 — popped stash · recreated worktree · marked td-024 open
```
