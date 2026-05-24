#!/usr/bin/env python3
"""assistant-judgement-subagent eval.

Verifies that the judgement subagent (bin/judgement-subagent.py):

  1. Actually reads the lessons index (lessons_read > 0).
  2. Cites the exact lesson ID that forbids/modifies a candidate (no
     hallucination).
  3. Returns a verdict for every candidate in the input batch (no missing
     IDs).
  4. Modifies a "use Opus on a one-line CSS change" candidate to use Sonnet
     instead, citing lesson lesson-1779489067-spawning-a-workspace-via-spawnclaudework.
  5. Approves a "use Opus on a design question" candidate without modification.
  6. Returns one JSON object on stdout, no extra prose.

Why this eval matters: the lesson system was previously a write-only journal
(use_count=1 across all 11 lessons, the touch-on-create). The subagent
restores the read path. If this eval ever fails we've lost steering.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBAGENT = REPO_ROOT / "bin/judgement-subagent.py"
EVAL_DIR = Path(__file__).resolve().parent
FIXTURE = EVAL_DIR / "fixtures/dispatch-batch.json"
LESSONS_INDEX = Path.home() / ".assistant/lessons/index.md"

# The dispatch lesson the subagent must cite when modifying td-101 (CSS tweak)
# and approving td-100 (design question). If the curator rewrites the lesson
# library, update this expectation.
EXPECTED_LESSON_PREFIX = "lesson-1779489067-spawning-a-workspace"


def fail(msg, *, output=None):
    print(f"\n❌ FAIL: {msg}")
    if output is not None:
        print("\n--- subagent output ---")
        print(json.dumps(output, indent=2))
    sys.exit(1)


def log(msg):
    print(f"[eval] {msg}")


def main():
    if not SUBAGENT.exists():
        fail(f"subagent not found at {SUBAGENT}")
    if not LESSONS_INDEX.exists():
        fail(
            f"lessons index missing at {LESSONS_INDEX}. "
            f"Run `bin/assistant-curator.py index` first."
        )
    if not FIXTURE.exists():
        fail(f"fixture missing at {FIXTURE}")

    log(f"calling subagent against fixture {FIXTURE}")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(SUBAGENT), "--input-file", str(FIXTURE)],
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("EVAL_TIMEOUT_SEC", "180")),
    )
    elapsed = time.time() - t0
    log(f"subagent finished in {elapsed:.1f}s rc={proc.returncode}")

    if proc.returncode != 0:
        fail(
            f"subagent exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )

    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        fail(f"subagent stdout is not valid JSON: {e}\nstdout:\n{proc.stdout}")

    if "_error" in out:
        fail(f"subagent returned error: {out['_error']}", output=out)

    # 1. Lessons were actually read.
    lessons_read = out.get("lessons_read", 0)
    if not isinstance(lessons_read, int) or lessons_read <= 0:
        fail(
            f"subagent did not read lessons (lessons_read={lessons_read!r}). "
            "This is the core failure mode the subagent exists to prevent.",
            output=out,
        )
    log(f"lessons_read={lessons_read} ✓")

    # 2. + 3. Verdicts present for every candidate.
    fixture = json.loads(FIXTURE.read_text())
    cand_ids = {c["id"] for c in fixture["candidates"]}
    verdicts = out.get("verdicts", {})
    missing = cand_ids - set(verdicts.keys())
    if missing:
        fail(f"missing verdicts for {sorted(missing)}", output=out)
    log(f"verdicts present for all {len(cand_ids)} candidates ✓")

    # 4. CSS tweak gets modify with model=sonnet citing the dispatch lesson.
    css = verdicts.get("td-101-css-tweak")
    if css is None:
        fail("missing verdict for td-101-css-tweak", output=out)
    if css["verdict"] != "modify":
        fail(
            f"expected verdict=modify for td-101-css-tweak (a CSS one-liner "
            f"shouldn't burn Opus tokens), got {css['verdict']!r}",
            output=out,
        )
    cited = css.get("applied_lessons", []) or []
    if not any(EXPECTED_LESSON_PREFIX in l for l in cited):
        fail(
            f"td-101 verdict didn't cite the dispatch-model lesson "
            f"(expected prefix '{EXPECTED_LESSON_PREFIX}', got {cited}). "
            f"This usually means the subagent hallucinated an ID or skipped "
            f"the index.",
            output=out,
        )
    if "sonnet" not in (css.get("modification") or "").lower():
        fail(
            f"td-101 modification text doesn't mention sonnet "
            f"(modification={css.get('modification')!r}). The lesson says "
            f"routine work goes to Sonnet 1M.",
            output=out,
        )
    log("td-101 CSS tweak: modify → MODEL=sonnet, cited dispatch lesson ✓")

    # 5. Design question gets approve, citing the same lesson (or no lesson).
    design = verdicts.get("td-100-design-question")
    if design is None:
        fail("missing verdict for td-100-design-question", output=out)
    if design["verdict"] != "approve":
        fail(
            f"expected verdict=approve for td-100-design-question (a design "
            f"decision IS what Opus is for), got {design['verdict']!r}",
            output=out,
        )
    log("td-100 design question: approve ✓")

    # 6. No hallucinated lesson IDs anywhere.
    actual_ids = {
        ln.split("`")[1]
        for ln in LESSONS_INDEX.read_text().splitlines()
        if ln.startswith("  - ID `") or "ID `" in ln
    }
    actual_ids = {x for x in actual_ids if x.startswith("lesson-")}
    log(f"index has {len(actual_ids)} known lesson IDs")
    for cid, v in verdicts.items():
        for cited_id in v.get("applied_lessons", []) or []:
            if cited_id not in actual_ids:
                fail(
                    f"verdict for {cid} cited non-existent lesson "
                    f"'{cited_id}' (hallucination). Known IDs: "
                    f"{sorted(actual_ids)[:3]}…",
                    output=out,
                )
    log("no hallucinated lesson IDs ✓")

    # Persist last passing run.
    last = EVAL_DIR / "last-run.json"
    last.write_text(json.dumps(out, indent=2))
    log(f"saved last-run.json → {last}")

    print(
        f"\n✅ PASS — subagent read {lessons_read} lessons, "
        f"approved design question, modified CSS tweak to Sonnet."
    )


if __name__ == "__main__":
    main()
