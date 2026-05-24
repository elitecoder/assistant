#!/usr/bin/env python3
"""judgement-subagent — fresh-context judge for Assistant's non-trivial actions.

The Assistant's main pulse runs in a long-lived Sonnet 1M session and accumulates
hundreds of KB of pulse history. Lessons read at boot get buried in attention.
This subagent solves that with a fresh context per pulse:

  Inputs (in order, top of attention):
    1. ~/.assistant/lessons/index.md        ← mandatory, read first
    2. The candidate-action batch (JSON)
    3. The relevant world slice (TODOs + live_sessions touching any candidate)

  Output:
    Per-candidate verdicts (approve / reject / modify) with cited lessons.
    Cross-action consistency (e.g. "you're dispatching td-30 AND emitting an
    awaiting card for it — pick one") because all candidates are in one call.

Usage from the main Assistant pulse:

    cat <<EOF | python3 ~/.claude/bin/judgement-subagent.py > /tmp/verdicts.json
    {
      "candidates": [
        {"id": "td-30", "kind": "dispatch", "summary": "...", "reasoning": "..."},
        {"id": "td-29", "kind": "mark-todo-status", "target_status": "done", ...}
      ],
      "world_slice": { ... TODOs + live_sessions ... }
    }
    EOF

  The Assistant then ONLY acts on candidates with verdict == "approve".

The subagent uses Sonnet 4.6 1M with --print (single turn, fresh process).
Latency: ~5-15s typical. Cost: cheap (one short turn per pulse, not per candidate).

Run with --self-test to validate the prompt + a fixture without spawning Claude
(useful in CI / pre-commit). Run normally to actually call the model.
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
LESSONS_INDEX = HOME / ".assistant/lessons/index.md"
LOG_DIR = HOME / ".assistant/judgement-log"

JUDGEMENT_SYSTEM_PROMPT = """You are the **Judgement Subagent** for the Assistant dispatcher.

Your job: review a batch of candidate actions the main Assistant wants to take, and
return a per-candidate verdict (approve / reject / modify). Each verdict MUST be
grounded in either:

  (a) An applicable lesson from `~/.assistant/lessons/index.md`, OR
  (b) An observable inconsistency in the candidate batch itself (e.g. two
      candidates that contradict each other), OR
  (c) An observable mismatch between a candidate's reasoning and the world slice.

You do NOT have access to long pulse history. That is a feature, not a bug —
your context is FRESH every call so lessons stay at the top of attention.

## Strict procedure

1. **Read lessons FIRST.** The index is at `~/.assistant/lessons/index.md`.
   Use the Read tool. Do not skip this.

2. **Read each candidate action carefully.** They arrive as JSON on stdin.

3. **Read the world_slice** for ground truth on TODOs / live sessions referenced
   by candidates.

4. **Decide each candidate:**
   - `approve`  — no lesson forbids this, world slice supports the reasoning,
                  no contradiction with sibling candidates.
   - `reject`   — a lesson explicitly forbids this OR the reasoning is
                  contradicted by the world slice OR a sibling candidate
                  makes this redundant/conflicting. Must cite which.
   - `modify`   — the action is mostly right but should be adjusted (e.g.
                  "spawn this dispatch but with MODEL=sonnet not opus per
                  lesson L"). Must include `modification` text.

5. **Output one JSON object** with the schema below to stdout. NOTHING else
   in stdout. (Use stderr / Read tool for thinking.)

## Output schema (exact)

```json
{
  "verdicts": {
    "<candidate-id>": {
      "verdict": "approve|reject|modify",
      "applied_lessons": ["<lesson-id>", ...],
      "reasoning": "<one or two sentences citing lesson or world-slice fact>",
      "modification": "<only present when verdict=modify>"
    },
    ...
  },
  "cross_action_notes": [
    "<optional: observations spanning multiple candidates>"
  ],
  "lessons_read": <int — how many lessons you actually read>
}
```

## Rules

- **Default to approve when no lesson applies.** You are not a second-guesser
  of the main Assistant's pulse logic; you are a lesson-applier and
  consistency-checker. If lessons are silent and the candidates are coherent,
  approve all of them.
- **Cite lesson IDs verbatim from the index** in `applied_lessons`. Do not
  paraphrase the IDs. If you're not sure of an ID, omit it (the eval will
  catch hallucinated IDs).
- **One subagent call per pulse, not per candidate.** Process the whole batch
  in a single response. Cross-action checks (e.g. dispatching td-X while
  ALSO emitting a needs-you card for it — pick one) are part of your job.
- **Do not propose new actions.** You only verdict the candidates given to you.
- **End with `lessons_read: <int>`** so the main Assistant can verify you
  actually read the index.

## Failure modes to avoid

- **Sycophancy** — approving everything because "it sounds reasonable".
  If a lesson says "never dispatch when X is true" and a candidate dispatches
  with X true, reject. The Assistant will surface a card; you don't need to
  protect feelings.
- **Hallucinating lesson IDs.** If you can't find a relevant lesson, the
  correct `applied_lessons` value is `[]`, not a fabricated ID.
- **Overriding the world slice with a guess.** The world slice is ground truth
  for TODO state and live sessions. If a candidate's reasoning says a TODO is
  open and the world slice shows it's done, reject and cite the slice.
"""


def read_lessons_index() -> str:
    if not LESSONS_INDEX.exists():
        return "(no lessons index — return lessons_read: 0)"
    return LESSONS_INDEX.read_text()


def build_user_message(payload: dict) -> str:
    """Compose the literal user message that goes to the subagent."""
    candidates = payload.get("candidates", [])
    world_slice = payload.get("world_slice", {})
    lessons = read_lessons_index()

    parts = [
        "## Step 1 — Lessons (READ FIRST)",
        "",
        "Below is the verbatim contents of `~/.assistant/lessons/index.md`.",
        "Apply any rule that touches the candidate actions below.",
        "",
        "```markdown",
        lessons,
        "```",
        "",
        "## Step 2 — Candidate actions",
        "",
        "```json",
        json.dumps(candidates, indent=2),
        "```",
        "",
        "## Step 3 — World slice",
        "",
        "TODOs and live_sessions referenced by the candidates above. This is",
        "ground truth — if a candidate's reasoning contradicts this slice,",
        "reject it.",
        "",
        "```json",
        json.dumps(world_slice, indent=2),
        "```",
        "",
        "## Step 4 — Output",
        "",
        "Emit ONE JSON object per the schema in the system prompt. Nothing",
        "else in stdout. End with `lessons_read: <int>`.",
    ]
    return "\n".join(parts)


def call_claude_subagent(user_message: str, model: str, log_path: Path) -> str:
    """Call `claude --print` and capture stdout. Logs the call for audit."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude")),
        "--print",
        "--model",
        model,
        "--append-system-prompt",
        JUDGEMENT_SYSTEM_PROMPT,
        "--output-format",
        "text",
    ]

    started = time.time()
    proc = subprocess.run(
        cmd,
        input=user_message,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("JUDGEMENT_TIMEOUT_SEC", "120")),
    )
    duration = time.time() - started

    log_path.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "duration_sec": round(duration, 2),
                "model": model,
                "exit_code": proc.returncode,
                "user_message": user_message,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            },
            indent=2,
        )
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}; stderr={proc.stderr[:400]}"
        )
    return proc.stdout


def parse_verdicts(stdout: str) -> dict:
    """Extract the JSON object from the subagent's stdout."""
    # The subagent may emit prose before/after the JSON. Find the first
    # well-formed object and parse it.
    s = stdout.strip()
    # Common case: pure JSON.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Fallback: find the first `{` and try increasing prefixes until parse works.
    start = s.find("{")
    if start < 0:
        raise ValueError(f"no JSON object found in subagent output:\n{s[:400]}")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s[start:])
        return obj
    except json.JSONDecodeError as e:
        raise ValueError(
            f"failed to decode JSON from subagent output: {e}\n"
            f"first 400 chars after '{{': {s[start:start+400]}"
        )


def validate_verdicts(verdicts: dict, candidates: list) -> list[str]:
    """Return a list of validation errors. Empty list = clean."""
    errs = []
    if "verdicts" not in verdicts:
        errs.append("missing 'verdicts' key")
        return errs
    cand_ids = {c["id"] for c in candidates}
    verdict_ids = set(verdicts["verdicts"].keys())
    missing = cand_ids - verdict_ids
    extra = verdict_ids - cand_ids
    if missing:
        errs.append(f"missing verdicts for: {sorted(missing)}")
    if extra:
        errs.append(f"verdicts for unknown candidates: {sorted(extra)}")
    for cid, v in verdicts["verdicts"].items():
        if v.get("verdict") not in ("approve", "reject", "modify"):
            errs.append(f"{cid}: invalid verdict {v.get('verdict')!r}")
        if v.get("verdict") == "modify" and not v.get("modification"):
            errs.append(f"{cid}: verdict=modify but no 'modification' text")
    if "lessons_read" not in verdicts:
        errs.append("missing 'lessons_read' field — subagent may not have read the index")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--model",
        default=os.environ.get("JUDGEMENT_MODEL", "us.anthropic.claude-sonnet-4-6[1m]"),
    )
    ap.add_argument("--self-test", action="store_true",
                    help="don't call the model — just validate the prompt against a fixture")
    ap.add_argument("--input-file", help="read JSON payload from a file instead of stdin")
    args = ap.parse_args()

    if args.self_test:
        # Construct a fixture and emit the would-be user-message so a human
        # (or eval) can inspect it.
        fixture = {
            "candidates": [
                {"id": "td-100", "kind": "dispatch",
                 "summary": "Spawn a workspace for td-100",
                 "reasoning": "autoDispatch=true, no in-flight match"},
            ],
            "world_slice": {"todos": [{"id": "td-100", "autoDispatch": True}],
                            "live_sessions": []},
        }
        msg = build_user_message(fixture)
        print(f"# system prompt is {len(JUDGEMENT_SYSTEM_PROMPT)} chars")
        print(f"# user message is {len(msg)} chars (lessons + candidates + world_slice)")
        print("---")
        print(msg)
        return 0

    if args.input_file:
        payload = json.loads(Path(args.input_file).read_text())
    else:
        payload = json.loads(sys.stdin.read())

    user_msg = build_user_message(payload)
    log_path = LOG_DIR / f"judgement-{int(time.time())}.json"

    try:
        stdout = call_claude_subagent(user_msg, args.model, log_path)
    except Exception as exc:
        print(json.dumps({
            "verdicts": {},
            "_error": f"subagent call failed: {exc}",
            "_log": str(log_path),
        }))
        return 1

    try:
        verdicts = parse_verdicts(stdout)
    except ValueError as exc:
        print(json.dumps({
            "verdicts": {},
            "_error": f"parse failed: {exc}",
            "_log": str(log_path),
        }))
        return 1

    errs = validate_verdicts(verdicts, payload.get("candidates", []))
    if errs:
        verdicts["_validation_errors"] = errs

    verdicts["_log"] = str(log_path)
    print(json.dumps(verdicts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
