#!/usr/bin/env python3
"""judgement-subagent — fresh-context judge for non-trivial Assistant actions.

The main Assistant pulse runs in a long-lived Sonnet 1M session and
accumulates hundreds of KB of pulse history. Rules read once at boot get
buried in attention. This subagent solves that with a fresh context per
pulse:

  Inputs (top of attention):
    1. ~/.claude/CLAUDE.md            ← auto-loaded by claude on every --print
    2. The candidate-action batch (JSON, on stdin)
    3. The relevant world slice (TODOs + live_sessions touching candidates)

  Output:
    Per-candidate verdicts (approve / reject / modify) with cited rule
    slugs. Cross-action consistency comes free because all candidates
    are evaluated in one call.

Lessons live in ~/.claude/CLAUDE.md inside a `## Lessons` section. Each
block is wrapped with `<!-- lesson: <slug>, scope: <scope>, added: <date> -->`
so verdicts can cite slugs back. CLAUDE.md auto-loads into every Claude
Code session (this is the official Claude Code primitive), so the subagent
sees the rules without us injecting them — but we DO instruct the subagent
to read the section first to keep them at the top of attention.

Usage from the main Assistant pulse:

    cat <<EOF | python3 ~/.claude/bin/judgement-subagent.py > /tmp/verdicts.json
    {
      "candidates": [
        {"id": "td-30", "kind": "dispatch", "summary": "...", "reasoning": "..."},
        ...
      ],
      "world_slice": { ... TODOs + live_sessions ... }
    }
    EOF

  The Assistant then ONLY acts on candidates with verdict == "approve".

Latency: ~10-20s typical (Sonnet 1M, single turn). Cost: cheap (one short
turn per pulse, not per candidate).
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
CLAUDE_MD = HOME / ".claude/CLAUDE.md"
LOG_DIR = HOME / ".assistant/judgement-log"

JUDGEMENT_SYSTEM_PROMPT = """You are the **Judgement Subagent** for the Assistant dispatcher.

Your job: review a batch of candidate actions the main Assistant wants to
take, and return a per-candidate verdict (approve / reject / modify). Each
verdict MUST be grounded in either:

  (a) An applicable rule from the `## Lessons` section of `~/.claude/CLAUDE.md`,
      OR
  (b) An observable inconsistency in the candidate batch itself (e.g. two
      candidates that contradict each other), OR
  (c) An observable mismatch between a candidate's reasoning and the world
      slice.

You do NOT have access to long pulse history. That is a feature, not a bug —
your context is FRESH every call so rules stay at the top of attention.

## Strict procedure

1. **Read the `## Lessons` section in `~/.claude/CLAUDE.md` FIRST.** It is
   auto-loaded into your context, but read it consciously to keep its rules
   at the top of attention. Each lesson block is wrapped with
   `<!-- lesson: <slug>, scope: <scope>, added: <date> -->` followed by
   `**trigger**` and a rule body. Cite slugs verbatim.

2. **Read each candidate action carefully.** They arrive as JSON on stdin.

3. **Read the world_slice** for ground truth on TODOs / live sessions
   referenced by candidates.

4. **Decide each candidate:**
   - `approve`  — no rule forbids this, world slice supports the reasoning,
                  no contradiction with sibling candidates.
   - `reject`   — a rule explicitly forbids this OR the reasoning is
                  contradicted by the world slice OR a sibling candidate
                  makes this redundant/conflicting. Must cite which rule.
   - `modify`   — the action is mostly right but should be adjusted (e.g.
                  "spawn this dispatch but with MODEL=sonnet not opus per
                  rule X"). Must include `modification` text.

5. **Output one JSON object** with the schema below to stdout. NOTHING else
   in stdout.

## Output schema (exact)

```json
{
  "verdicts": {
    "<candidate-id>": {
      "verdict": "approve|reject|modify",
      "applied_lessons": ["<slug>", ...],
      "reasoning": "<one or two sentences citing rule or world-slice fact>",
      "modification": "<only present when verdict=modify>"
    },
    ...
  },
  "cross_action_notes": [
    "<optional: observations spanning multiple candidates>"
  ],
  "lessons_read": <int — how many lesson blocks you actually read>
}
```

## Rules

- **Default to approve when no rule applies.** You are not a second-guesser
  of the main Assistant's pulse logic; you are a rule-applier and
  consistency-checker. If the rules are silent and the candidates are
  coherent, approve all of them.
- **Cite slugs verbatim from the lesson block headers.** Lesson blocks
  begin with `<!-- lesson: <slug>, scope: <scope>, added: <date> -->`.
  Copy the slug *exactly* — do not capitalize it, do not strip the scope
  prefix, do not paraphrase. Example: a header that says
  `<!-- lesson: ffp-never-skip-g3, ... -->` produces `applied_lessons:
  ["ffp-never-skip-g3"]`. NOT `["never-skip-G3"]`, NOT `["never-skip-g3"]`,
  NOT `["ffp:never-skip-g3"]`. The slug is a literal string match —
  audits and evals check it character-for-character.
- When your `reasoning` references a rule, the corresponding slug MUST
  appear in `applied_lessons`. An empty `applied_lessons` together with
  reasoning that quotes a rule is a bug — fix it before emitting.
- **If you can't find a relevant rule**, the correct `applied_lessons`
  value is `[]`, AND your reasoning must NOT cite a rule. Ground in the
  world slice or sibling-candidate inconsistency instead.
- **One subagent call per pulse, not per candidate.** Process the whole
  batch in a single response.
- **Do not propose new actions.** You only verdict the candidates given
  to you.
- **End with `lessons_read: <int>`** so the main Assistant can verify
  you actually read the section.

## Failure modes to avoid

- **Sycophancy** — approving everything because "it sounds reasonable".
  If a rule says "never X" and a candidate does X, reject.
- **Hallucinating slugs.** If you can't find a relevant rule, return `[]`.
- **Overriding the world slice with a guess.** The world slice is ground
  truth for TODO state and live sessions.
"""


def extract_lessons_section() -> str:
    """Read CLAUDE.md and return just the Lessons section (heading + body)."""
    if not CLAUDE_MD.exists():
        return "(no ~/.claude/CLAUDE.md found)"
    text = CLAUDE_MD.read_text()
    import re as _re
    m = _re.search(r"^## Lessons\b.*", text, _re.MULTILINE)
    if not m:
        return "(no ## Lessons section in CLAUDE.md)"
    start = m.start()
    next_h2 = _re.search(r"^## ", text[m.end():], _re.MULTILINE)
    end = m.end() + next_h2.start() if next_h2 else len(text)
    return text[start:end].rstrip()


def build_user_message(payload: dict) -> str:
    candidates = payload.get("candidates", [])
    world_slice = payload.get("world_slice", {})
    lessons_section = extract_lessons_section()
    parts = [
        "## Step 1 — Lessons (verbatim from `~/.claude/CLAUDE.md`)",
        "",
        "Apply any rule below that touches the candidate actions. When you ",
        "cite a rule in `applied_lessons`, copy the slug verbatim from the ",
        "`<!-- lesson: ... -->` comment header. The slug is between `lesson: ` ",
        "and `,` — copy that exact substring, do NOT paraphrase or normalize.",
        "",
        "```markdown",
        lessons_section,
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
        "else in stdout. End with `lessons_read: <int>` (count of `<!-- lesson:`",
        "block headers you read).",
    ]
    return "\n".join(parts)


def call_claude_subagent(user_message: str, model: str, log_path: Path) -> str:
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
    s = stdout.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
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
        errs.append("missing 'lessons_read' field — subagent may not have read the section")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--model",
        default=os.environ.get("JUDGEMENT_MODEL", "us.anthropic.claude-sonnet-4-6[1m]"),
    )
    ap.add_argument("--self-test", action="store_true",
                    help="don't call the model — just emit the would-be user message")
    ap.add_argument("--input-file", help="read JSON payload from a file instead of stdin")
    args = ap.parse_args()

    if args.self_test:
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
        print(f"# user message is {len(msg)} chars")
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
