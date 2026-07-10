---
name: goal
description: Add, list, rerank, and update Mukul's ranked goals at ~/.claude/assistant-goals.json — the Keel goals control loop the deterministic planner (bin/plan-next-actions.py) reads every pulse. Use when the user types /goal, asks to "add a goal", "list goals", "rerank goals", "pause the planner", or wants to change a goal's outcome/links/status. Mutations go through the local todo-server routes (POST-only, localhost) so goals.json stays flock'd and atomically written. Automation NEVER edits goals directly — it files a confirmation-gated goal_update proposal; only a human (this skill) edits in place.
---

# /goal — ranked goals + planner control loop

Ranked goal store for the Assistant. File: `~/.claude/assistant-goals.json` (schema in the `_schema` field). The deterministic planner reads it every pulse, stamps mechanical progress, and stages the next playbook step for stalled goals. This skill is the **human** edit path; it drives the todo-server's `/goal/*` routes (localhost `http://127.0.0.1:9876`), which own the flock'd atomic writes.

## Subcommands

```
/goal                              list goals in rank order
/goal list                         same
/goal add "<title>" --outcome "<measurable outcome>" [flags]
/goal update goal-N --status done|active|paused
/goal update goal-N --outcome "<text>"
/goal rerank goal-3 goal-1 goal-2  set that as ranks 1,2,3
/goal pause | /goal resume         flip the _paused kill switch
```

### Add flags

- `--outcome "<text>"` — **required**, must be measurable
- `--horizon "<text>"` — e.g. `Q3`, `2wk`
- `--repo <owner/name>` (repeatable), `--pr <num>` (repeatable), `--todo td-NNN` (repeatable), `--channel <id>`, `--sender <addr>`, `--jql "<query>"` — the mechanical `links` the progress-linker matches on
- `--rank N` — insert at a rank (default: appended last)

## Execution

All mutations are POST to the local todo-server. Read-only `list` is also POST (mirrors `/decision/list`). The server re-renders the dashboard after each write.

```bash
BASE=http://127.0.0.1:9876

# list
curl -s -X POST $BASE/goal/list | python3 -m json.tool

# add  (body = JSON)
curl -s -X POST $BASE/goal/add -H 'Content-Type: application/json' \
  -d '{"title":"Ship Keel M5 connectors","outcome":"GitHub+Gmail connectors green in prod, mis-lane <10%","horizon":"Q3","links":{"repos":["elitecoder/assistant"]}}'

# update  (body = changes)
curl -s -X POST $BASE/goal/update/goal-1 -H 'Content-Type: application/json' \
  -d '{"status":"done"}'

# rerank
curl -s -X POST $BASE/goal/rerank -H 'Content-Type: application/json' \
  -d '{"order":["goal-3","goal-1","goal-2"]}'
```

`pause`/`resume` set `_paused` via an update-style call:

```bash
curl -s -X POST $BASE/goal/update/goal-1 -H 'Content-Type: application/json' -d '{}'  # no-op ping
# The kill switch lives at the store root; edit it via the goals module if the
# server is down:  python3 -c "import sys;sys.path.insert(0,'~/dev/assistant/src');from assistant import goals;goals.set_paused(True)"
```

## Guardrails

- **Outcome is mandatory and measurable** — the store rejects a goal with no outcome. "Make progress on X" is not an outcome; "X shipped to prod with test coverage ≥80%" is.
- **`lastProgressAt` is mechanical** — never set it by hand; the planner stamps it from the ledger/PRs/decisions/TODOs. This skill cannot edit it.
- **Rank is unique** — `rerank` reassigns 1..N; unlisted goals keep their relative order after the listed ones.
- **Automation is confirmation-gated** — if a pulse decides a goal "looks done" or should be reranked, it files a `goal_update` proposal (type=`goal_update` in `proposals.jsonl`) for Mukul to confirm in the brief. It never edits the store. Only this human skill edits directly.
- **`_paused:true` is the kill switch** — the planner no-ops (ledgered) until resumed.

## Failure handling

- **Server down** (connection refused) → tell the user the todo-server LaunchAgent isn't running; the store can still be inspected at `~/.claude/assistant-goals.json`.
- **Validation error** (400) → surface the message (missing outcome, unknown field, goal not found) and stop; don't retry.
