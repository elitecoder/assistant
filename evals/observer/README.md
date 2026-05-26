# Observer eval suite

Verifies the Observer agent emits the right verdict for representative
workspace states. Each fixture is one test case.

## Run

```bash
./run.py                       # all fixtures
./run.py 01-ws97-trap-no-pr-mid-audit
./run.py --list                # list fixture names
EVAL_MODEL=us.anthropic.claude-sonnet-4-6[1m] ./run.py
```

`run.py` spawns a one-shot headless `claude --print` per fixture, feeding
the Observer prompt + a pointer to `ctx.json`. Observer reads the
fixture's `transcript.jsonl` directly via bash. If the fixture has a
`fake-gh-bin/`, that's PATH-prepended so `gh pr view` calls hit a shim.

The runner extracts the last JSON line from stdout, asserts the verdict
kind matches `expected.json`, and for `stranded` / `needs_user` checks
the required payload fields are non-empty. Verdict text content is not
checked — that's deliberate (we want the right shape, not a specific
phrase).

## Fixtures

| # | Name | What it pins |
|---|---|---|
| 01 | ws97-trap-no-pr-mid-audit | The actual production-bug transcript. Observer must NOT auto-cleanup mid-audit. |
| 02 | test-only-pr-ci-green | Rule A1: test/E2E-only files + green CI → ready_for_merge. |
| 03 | refactor-pr-ci-green | Rule A2: `[REFACTOR]` title + green CI → ready_for_merge. |
| 04 | feature-pr-ci-green-needs-review | Rule A3: feature PR + green CI → needs_user (no auto-merge). |
| 05 | done-recap-no-pr | Rule B1: definitive recap + clean cwd → ready_for_cleanup. |
| 06 | question-to-user | Rule B2: agent asked a question → needs_user. |
| 07 | stranded-mid-task | Rule B3: idle >30min mid-narrative → stranded with nudge. |
| 08 | active-working | Rule B4 default: working / recent → active. |
| 09 | adversarial-pr-prose-no-pr-actually | Adversarial: prose mentions a merged PR but workspace owns no PR. Must NOT auto-act on the prose-derived PR. |

## Fixture layout

```
fixtures/<name>/
  ctx.json              # what build-ws-context.py would emit
  transcript.jsonl      # the Claude session JSONL Observer reads
  expected.json         # {"verdict": "..."} expected
  fake-gh-bin/gh        # optional: gh shim for cases that need PR data
  notes.md              # what this case proves
```

## Adding a fixture

1. Copy a real session jsonl from `~/.claude/projects/<slug>/<id>.jsonl`,
   or hand-write one.
2. `ctx.json` — the JSON `build-ws-context.py` would emit for this state
   (mostly `last_turn_age_sec`, `agent_status`, `cwd_dirty`,
   `cwd_unpushed`, `transcript_path` placeholder).
3. `expected.json` — `{"verdict": "kind"}`.
4. Optional `fake-gh-bin/gh` — Python shim that returns mock `gh pr view`
   output. Make it `chmod +x`.
5. `notes.md` — what behavior this case enforces.
