#!/usr/bin/env python3
"""world-observer-subagent — fresh-context observer for Assistant's pulse.

The main Assistant pulse used to read world.json + iterate live_sessions +
read transcripts + run gh pr view + classify each session itself. That
work accumulated ~30-60KB of context per pulse — the model collapsed by
pulse 200.

This subagent does ALL the observation work in a fresh process per pulse,
returning a structured JSON report. The main pulse becomes a thin
coordinator: orchestrate (this) → judgement subagent → persist state.

  Inputs (passed by the main pulse):
    --world-path  path to ~/.claude/cache/world.json
    --todo-path   path to ~/.claude/assistant-todo.json
    --state-path  path to PRIOR pulse's state file (for awaiting-card dedup)
    --pulse-idx   the upcoming pulse index

  The subagent itself reads:
    - the three input files above
    - any transcripts referenced via cmux-registry
    - gh pr view (for PRs cited in transcripts)
    - cmux list-workspaces

  Output (stdout, ONE JSON object):
    {
      "_meta": {"pulse_idx": ..., "n_sessions_reviewed": ..., "lessons_read": ...},
      "candidate_actions": [
        {"id": "<stable-key>", "kind": "dispatch|status-flip|cleanup|merge-pr|nudge|emit-card",
         "summary": "...", "reasoning": "...", "params": {...}}
      ],
      "draft_awaiting_cards": [
        {"key": "...", "tier": "T2|T3", "title": "...", "detail": "...",
         "touches": [...], "alt_actions": [...], "confidence": 0.0}
      ]
    }

  The main pulse passes `candidate_actions` to the judgement subagent;
  approved ones get acted on; both lists get persisted to assistant-state.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HOME"])
LOG_DIR = HOME / ".assistant/world-observer-log"

OBSERVER_SYSTEM_PROMPT = """You are the **World Observer Subagent** for the Assistant dispatcher.

Your job: read the current state of the user's cmux/Claude environment and
emit a structured report describing what the main Assistant should do this
pulse. You do NOT take actions — you propose them. The judgement subagent
will check your proposals against rules, then the main pulse will execute
the approved ones.

## Your inputs

The main pulse passes these on stdin as a JSON object:

```json
{
  "world_path": "~/.claude/cache/world.json",
  "todo_path": "~/.claude/assistant-todo.json",
  "state_path": "~/.claude/cache/assistant-state.json",
  "pulse_idx": 42
}
```

You should read those files yourself using the Read tool, plus any
transcripts they point to via `transcript_path`, plus run shell commands as
needed:

- `gh pr view <N> --json state,reviewDecision,statusCheckRollup,mergeable -q '.'`
- `cmux list-workspaces`
- `cmux rpc surface.read_text` (only when transcript is unavailable)

Lessons from `~/.claude/CLAUDE.md` are auto-loaded into your context. Read
them before classifying sessions or proposing actions.

## What you produce

ONE JSON object on stdout. Schema:

```json
{
  "_meta": {
    "pulse_idx": 42,
    "n_sessions_reviewed": 14,
    "lessons_read": 12,
    "active_workspace_count": 5,
    "total_workspace_count": 28
  },
  "candidate_actions": [
    {
      "id": "td-NNN-dispatch-1",
      "kind": "dispatch|status-flip|cleanup|merge-pr|nudge|emit-card|close-workspace",
      "summary": "<one sentence>",
      "reasoning": "<why this action; cite transcript/PR-state evidence>",
      "params": {
        "td": "td-NNN", "ws_ref": "workspace:N", "pr": 11042,
        "target_status": "done|deferred", "model": "sonnet|opus", ...
      },
      "evidence": "<verbatim quote from transcript or gh output>"
    }
  ],
  "draft_awaiting_cards": [
    {
      "key": "assistant:needs-you:...",
      "tier": "T2|T3",
      "title": "...",
      "detail": "...",
      "touches": [{"type":"session|todo","ref":"workspace:N|td-NNN","name":"..."}],
      "alt_actions": ["..."],
      "confidence": 0.7
    }
  ]
}
```

## Procedure

1. **Read the three input files** (world, todos, prior state).
2. **Count workspaces** — call `cmux list-workspaces` and count cmux tabs.
   If total_workspace_count >= 30, you MUST NOT propose any `dispatch`
   actions — only status-flips, cleanups, merges, awaiting cards. Add a
   `draft_awaiting_card` with key `assistant:dispatch-cap-hit:total-30`
   summarizing the cap hit.
3. **Iterate live_sessions** (from world.json). For each, classify:
   - Read its `transcript_path` JSONL — last ~50 turns.
   - Look for: PR opened (extract PR#), PR merged, work complete, awaiting
     user, agent silent for hours, broken/error state.
   - For PRs cited, run `gh pr view <N>` to get current state.
   - Build candidate_actions for: status-flip (TODO done/deferred/blocked),
     cleanup (PR merged), merge-pr (test-only or refactor PR with passing
     tests — see lessons), or emit-card (needs-you / cleanup-gated).
4. **Iterate TODOs** with autoDispatch=true and no in-flight workspace.
   Build dispatch candidate_actions, but RESPECT the cap (rule 2).
5. **Re-validate prior pulse's awaiting_input** — for each card, re-derive
   its predicate against current state. If still valid, include it in
   draft_awaiting_cards. If invalidated, EMIT a `purge-awaiting` candidate
   action so the main pulse logs the drop.
6. **Output ONE JSON object** to stdout. Nothing else.

## Hard rules

- **Never propose closing workspaces 3 (Manager) or 108 (Assistant agent)
  or any cron/long-lived role workspace** (E2E Reliability, etc).
- **Never propose dispatch when total_workspace_count >= 30** — emit a
  cap-hit card instead.
- **Never propose actions you cannot ground in transcript or gh evidence.**
  An empty `evidence` field is a bug — leave the action out.
- **Default to emitting awaiting-cards over taking actions.** The main
  pulse + judgement subagent are the ones that authorize action; you
  propose, they decide.
- **Lessons in CLAUDE.md are constraints on you too.** If a lesson forbids
  an action pattern (e.g. "never skip G3"), don't propose it.

## Failure modes to avoid

- Hallucinating PR states. Always run `gh pr view` rather than trusting
  the transcript's recap of "merged" / "green".
- Missing the workspace-count cap. Count cmux tabs FIRST, then decide
  whether dispatch candidates are allowed.
- Producing actions without evidence. The main pulse audits `evidence`;
  empty fields get the action rejected.
"""


def call_subagent(payload: dict, model: str, log_path: Path) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    user_msg = json.dumps(payload, indent=2)
    cmd = [
        os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude")),
        "--print",
        "--model", model,
        "--append-system-prompt", OBSERVER_SYSTEM_PROMPT,
        "--output-format", "text",
        "--add-dir", str(HOME / ".claude"),
        "--add-dir", str(HOME / ".assistant"),
        "--dangerously-skip-permissions",
    ]
    started = time.time()
    proc = subprocess.run(
        cmd,
        input=user_msg,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("OBSERVER_TIMEOUT_SEC", "300")),
    )
    duration = time.time() - started
    log_path.write_text(json.dumps({
        "ts": time.time(),
        "duration_sec": round(duration, 2),
        "model": model,
        "exit_code": proc.returncode,
        "user_message": user_msg,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }, indent=2))
    if proc.returncode != 0:
        raise RuntimeError(f"observer exited {proc.returncode}; stderr={proc.stderr[:400]}")
    return proc.stdout


def parse_report(stdout: str) -> dict:
    s = stdout.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in observer output:\n{s[:400]}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(s[start:])
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model",
                    default=os.environ.get("OBSERVER_MODEL",
                                           "us.anthropic.claude-sonnet-4-6[1m]"))
    ap.add_argument("--world-path", default=str(HOME / ".claude/cache/world.json"))
    ap.add_argument("--todo-path", default=str(HOME / ".claude/assistant-todo.json"))
    ap.add_argument("--state-path", default=str(HOME / ".claude/cache/assistant-state.json"))
    ap.add_argument("--pulse-idx", type=int, required=True)
    args = ap.parse_args()

    payload = {
        "world_path": args.world_path,
        "todo_path": args.todo_path,
        "state_path": args.state_path,
        "pulse_idx": args.pulse_idx,
    }
    log_path = LOG_DIR / f"observer-{int(time.time())}.json"
    try:
        stdout = call_subagent(payload, args.model, log_path)
    except Exception as exc:
        print(json.dumps({
            "_error": f"observer call failed: {exc}",
            "_log": str(log_path),
            "candidate_actions": [],
            "draft_awaiting_cards": [],
        }))
        return 1
    try:
        report = parse_report(stdout)
    except ValueError as exc:
        print(json.dumps({
            "_error": f"parse failed: {exc}",
            "_log": str(log_path),
            "candidate_actions": [],
            "draft_awaiting_cards": [],
        }))
        return 1
    report["_log"] = str(log_path)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
