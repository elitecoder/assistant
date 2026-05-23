---
name: todo
description: Add, list, and update items in Mukul's persistent TODO list at ~/.claude/assistant-todo.json. Use when the user types /todo, asks to "add a todo", "mark td-NNN done", "defer td-NNN", "remove td-NNN", or wants to see the current TODO list. Single source of truth — Triage agent reads from the same file. After any mutation, the dashboard auto-refreshes within ~15s; nudge Triage with a pulse for sub-minute freshness.
---

# /todo — TODO management

Persistent TODO list for the Assistant system. File: `~/.claude/assistant-todo.json`. Schema documented in the file's `_schema` field. Triage agent reads this file on every pulse; dashboard renderer surfaces it on the **TODOs** tab.

## Subcommands

```
/todo                              list open items, grouped by priority
/todo P1 "<title>" [flags]         add a new item
/todo done td-NNN                  flip status to "done"
/todo defer td-NNN                 flip status to "deferred"
/todo show td-NNN                  print full record
/todo rm td-NNN                    soft-remove (moves to removed[] with timestamp)
/todo list <P0|P1|P2|P3|P4>        list one priority bucket
```

### Add flags

- `--no-auto` — set `autoDispatch: false` (opt out — for design rules, discipline reminders, and other not-actually-dispatchable items)
- `--detail "<text>"` — longer description shown on hover/expand
- `--ws ws:N` — link to an existing workspace (sets `dispatchedWs`)
- `--source "<text>"` — override default source (which is `manual:<YYYY-MM-DD>`)

**Default: `autoDispatch=true`.** New TODOs are agent-dispatchable by default — Triage will spawn a workspace for them on the next pulse. Pass `--no-auto` to opt out for items that aren't actually shippable work (e.g. "be careful about X" rules, items waiting on a product decision). The Triage in-flight check prevents duplicate dispatches when a workspace is already shipping the same work.

## Execution

All work is filesystem-side — read JSON, mutate, atomic write. No HTTP, no subprocess except the Triage repulse at the end.

### Step 1 — parse args

Extract subcommand. Validate priority is one of `P0/P1/P2/P3/P4`. Title must be non-empty for `add`. ID must match `^td-\d{3,4}$` for `done`/`defer`/`show`/`rm`.

### Step 2 — read + mutate the JSON file atomically

```python
import json, os, shutil, sys
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(os.path.expanduser("~/.claude/assistant-todo.json"))
data = json.loads(PATH.read_text())
items = data.setdefault("items", [])
completed = data.setdefault("completed", [])
removed = data.setdefault("removed", [])

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def next_id():
    used = set()
    for bucket in (items, completed, removed):
        for it in bucket:
            m = (it.get("id") or "").lstrip("td-")
            try: used.add(int(m))
            except ValueError: pass
    n = max(used) + 1 if used else 1
    return f"td-{n:03d}"

def find(tid):
    for bucket_name, bucket in (("items", items), ("completed", completed), ("removed", removed)):
        for it in bucket:
            if it.get("id") == tid:
                return bucket_name, bucket, it
    return None, None, None

def write_atomic():
    data["_lastUpdated"] = now_iso()
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(PATH)
```

### Step 3 — apply the operation

**add** (with de-dup pre-check):
```python
# De-dup: don't create a TODO that overlaps an existing OPEN item.
# Triage-side incident 2026-05-22: td-019 was created from a closed-workspace
# audit, then a similar td had already been hand-added; downstream auto-dispatch
# spawned two workspaces shipping the same PR. De-dup at creation time prevents
# the duplicate from existing.
import re

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "by",
    "is", "was", "are", "with", "from", "as", "at", "this", "that",
    "fix", "feat", "feature", "todo", "task", "work", "auto", "ws",
    "squirrel", "ffp", "pr",  # too common in this project
}

def normalize_title(s: str) -> set:
    """Token set for similarity scoring."""
    s = (s or "").lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9\-]+", s)
    return {t for t in tokens if t not in STOPWORDS and len(t) >= 3}

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

# Build candidate set from currently-OPEN (or in-progress) TODOs.
new_tokens = normalize_title(title) | normalize_title(detail or "")
duplicates = []
for it in items:
    if it.get("status") in ("done", "deferred"):
        continue
    existing_tokens = normalize_title(it.get("title", "")) | normalize_title(it.get("detail", ""))
    score = jaccard(new_tokens, existing_tokens)
    if score >= 0.4:  # 40%+ token overlap → likely the same work
        duplicates.append((score, it))

if duplicates:
    duplicates.sort(reverse=True)
    print("⚠️  Possible duplicate(s) of this title in OPEN TODOs:")
    for score, it in duplicates[:3]:
        print(f"     [{score:.0%} overlap] {it['id']} ({it.get('status','open')}) — {it.get('title','')[:80]}")
    print("")
    if not (auto and os.environ.get("TODO_FORCE_DEDUP_BYPASS") == "1"):
        # Hard stop unless caller explicitly bypassed (Triage's own auto-create
        # path can pass TODO_FORCE_DEDUP_BYPASS=1 after Triage has confirmed the
        # match isn't real — but the default for human /todo invocations is to
        # block).
        print("Aborting: edit the existing TODO instead, or pass TODO_FORCE_DEDUP_BYPASS=1 to override.")
        sys.exit(1)
    print("(TODO_FORCE_DEDUP_BYPASS=1 set — proceeding anyway.)")

tid = next_id()
# Default autoDispatch=True. Caller passes --no-auto to opt out (sets to False).
# An explicit `auto` flag of None should default to True; only False flips it off.
auto_dispatch = False if no_auto else True
new_item = {
    "id": tid,
    "priority": priority,
    "title": title,
    "source": source or f"manual:{datetime.now(timezone.utc).date().isoformat()}",
    "createdAt": datetime.now(timezone.utc).date().isoformat(),
    "status": "open",
    "autoDispatch": auto_dispatch,
}
if detail: new_item["detail"] = detail
if ws: new_item["dispatchedWs"] = ws
items.append(new_item)
write_atomic()
flag_note = "autoDispatch" if auto_dispatch else "manual-only"
print(f"✓ {tid} added ({priority}, status=open, {flag_note})")
```

**done / defer**:
```python
bucket_name, bucket, it = find(tid)
if not it: sys.exit(f"not found: {tid}")
new_status = "done" if subcommand == "done" else "deferred"
it["status"] = new_status
it["statusUpdatedAt"] = now_iso()
write_atomic()
print(f"✓ {tid} → {new_status}")
```

**rm**:
```python
bucket_name, bucket, it = find(tid)
if not it: sys.exit(f"not found: {tid}")
if bucket_name == "removed":
    sys.exit(f"already removed: {tid}")
it["removedAt"] = now_iso()
bucket.remove(it)
removed.append(it)
write_atomic()
print(f"✓ {tid} removed")
```

**show**:
```python
bucket_name, _, it = find(tid)
if not it: sys.exit(f"not found: {tid}")
print(f"# {tid} (in {bucket_name})")
print(json.dumps(it, indent=2))
```

**list (no args, or with priority filter)**:
```python
filt = priority_filter  # None or "P0".."P4"
buckets = {p: [] for p in ("P0","P1","P2","P3","P4")}
for it in items:
    if it.get("status") in ("done","deferred"): continue
    p = it.get("priority","P3")
    if filt and p != filt: continue
    buckets.setdefault(p, []).append(it)
for p in ("P0","P1","P2","P3","P4"):
    if not buckets[p]: continue
    print(f"## {p}")
    for it in buckets[p]:
        flags = " ⚡auto" if it.get("autoDispatch") else ""
        print(f"  {it['id']}  [{it.get('status','open'):<11}]  {it.get('title','')[:80]}{flags}")
```

### Step 4 — nudge Triage so the new TODO surfaces in <2 min

For `add` / `done` / `defer` / `rm` (any mutation), run:

```bash
~/.claude/bin/triage-pulse.sh 2>/dev/null || true
```

Don't block on output. The pulse fires `pulse-now` to the Triage workspace; Triage's next emission will reflect the change. Dashboard auto-refreshes the tab within 15s.

For `list` / `show` (read-only), skip the pulse.

### Step 5 — confirm to user

Single line. ID, priority, what changed. No verbose output.

## Examples

```
/todo P1 "Fix the dashboard refresh bug"
→ ✓ td-031 added (P1, status=open, autoDispatch)

/todo P2 "Watch out for ECS coupling to Selection in Squirrel" --no-auto
→ ✓ td-032 added (P2, status=open, manual-only)

/todo P0 "ws:117 needs review" --ws ws:117
→ ✓ td-033 added (P0, status=open, autoDispatch)

/todo done td-024
→ ✓ td-024 → done

/todo
→ ## P0
     td-002  [open       ]  Resolve 6-day-stale cleanup scope question (ws:1)
   ## P1
     td-003  [open       ]  Squirrel Copy/Paste — answer 4 design questions before Phase 1
     ...

/todo show td-024
→ # td-024 (in items)
   { "id": "td-024", "priority": "P0", ... full JSON ... }
```

## Guardrails

- **Atomic writes** — always tmp-file + rename, never partial writes that Triage would read.
- **Never delete** — `rm` moves to `removed[]` with timestamp; the file's removed bucket is the recovery rope.
- **ID is monotonic** — `next_id()` scans all three buckets so a new TODO never collides with a removed/completed one.
- **status is one of**: `open / in-progress / blocked / done / deferred / stale`. The skill writes `open` on add, `done`/`deferred` on flip; Triage owns transitions to `in-progress`/`blocked`/`stale`.
- **Don't break legacy completed[]** — items with `status=done` may live in either `items[]` (new) or `completed[]` (legacy). `find()` handles both.

## Failure handling

- **JSON parse error** → don't write; tell user file is corrupt at `~/.claude/assistant-todo.json` and stop.
- **ID not found** → exit non-zero with `not found: <tid>`. Don't fuzzy-match — if user typed wrong ID, that's their error to fix.
- **Triage pulse fails** → log silently, don't block the success message. The TODO is on disk regardless.
