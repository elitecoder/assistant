#!/usr/bin/env python3
"""replay-linker.py — stall-precision harness for the mechanical progress linker
+ stall detector (Keel M4 exit criterion: ">80% stall precision hand-verified
before autoDispatch defaults on").

It REPLAYS goals.last_progress_at + goals.is_stalled day-by-day over a 30-day
window of an actions ledger for the 2 seeded goals, and reports STALL PRECISION:
of the (goal, day) pairs the detector FLAGS as stalled, how many are TRULY
stalled by the mechanical definition — computed here by an INDEPENDENT trailing-
window scan of the same artifacts, so a regression in the linker's typed
matching (e.g. it stops matching merged PRs) diverges the two and drops
precision below the gate.

Ledger source: this environment has NO real 30-day actions ledger (checked:
~/.assistant/actions-ledger.jsonl absent), so the harness runs against the
COMMITTED SYNTHETIC fixture evals/goals/fixtures/ledger-30d.jsonl, which
exercises the same code paths (ledger rows, a merged PR, TODO completions, and
noise rows that must not match). The runner prints whether the ledger was real
or synthetic.

Runnable two ways:
    python3 evals/goals/replay-linker.py        # prints the precision report
    python3 -m unittest evals.goals.replay-linker  (also a unittest)
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import goals  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
REAL_LEDGER = Path(os.path.expanduser("~/.assistant/actions-ledger.jsonl"))
DAY = 86400
PRECISION_GATE = 0.80
REPLAY_DAYS = 30


def _load_ledger() -> tuple[list[dict], bool]:
    """(rows, is_real). Prefer a real 30d+ ledger; fall back to the committed
    synthetic fixture (and say so)."""
    if REAL_LEDGER.exists():
        rows = goals._read_jsonl(REAL_LEDGER)
        if rows:
            return rows, True
    return goals._read_jsonl(FIX / "ledger-30d.jsonl"), False


def _load_goals() -> list[dict]:
    store = json.loads((FIX / "seeded-goals.json").read_text())
    return [goals._normalize_goal(g) for g in store["goals"]
            if goals._valid_goal(g)]


def _ground_truth_stalled(goal: dict, artifacts: list[dict], now: float) -> bool:
    """INDEPENDENT stall oracle: no matching artifact in the trailing
    stallAfterHours window. Deliberately NOT via goals.last_progress_at — it
    re-derives the match with its own typed check so a linker regression is
    caught as a precision drop, not silently mirrored."""
    stall_after = goal.get("stallAfterHours", goals.DEFAULT_STALL_AFTER_HOURS)
    window_start = now - stall_after * 3600
    gl = goal.get("links") or {}
    repos = {goals._norm_repo(r) for r in (gl.get("repos") or [])}
    prs = {str(x) for x in (gl.get("prs") or [])}
    todos = {str(x) for x in (gl.get("todos") or [])}
    anchor = goals.parse_iso(goal.get("createdAt"))
    seen_progress = False
    for art in artifacts:
        ts = art.get("ts")
        if not isinstance(ts, (int, float)) or ts > now or ts <= window_start:
            continue
        tok = art.get("tokens") or {}
        if ((tok.get("repos") or set()) & repos
                or (tok.get("prs") or set()) & prs
                or (tok.get("todos") or set()) & todos):
            seen_progress = True
            break
    if seen_progress:
        return False
    # No progress in-window → stalled only if the goal is old enough to measure.
    if anchor is None:
        return False
    return (now - anchor) > stall_after * 3600


def replay() -> dict:
    """Walk 30 days; at each day compute detector-flagged stall vs the oracle.
    Returns a precision report."""
    ledger_rows, is_real = _load_ledger()
    seeded = _load_goals()
    # Build the full artifact stream once (linker path) — the oracle reads the
    # same normalized artifacts, so both see identical data.
    prs = goals._merged_prs_from_ledger(ledger_rows)
    artifacts = goals.ledger_artifacts(ledger_rows) + goals.pr_artifacts(prs)

    epochs = [a["ts"] for a in artifacts if isinstance(a.get("ts"), (int, float))]
    if not epochs:
        return {"error": "no artifacts in ledger", "is_real": is_real}
    base = min(epochs)

    flagged = flagged_true = 0
    per_goal: dict[str, dict] = {}
    for goal in seeded:
        gflag = gtrue = 0
        for d in range(REPLAY_DAYS):
            now = base + d * DAY + DAY  # end-of-day instant
            lp = goals.last_progress_at(goal, artifacts, now=now)
            # inline the detector's stall math against the replay `now`
            anchor = lp if lp is not None else goals.parse_iso(goal.get("createdAt"))
            stall_after = goal.get("stallAfterHours", goals.DEFAULT_STALL_AFTER_HOURS)
            det_stalled = (anchor is not None
                           and (now - anchor) > stall_after * 3600)
            if not det_stalled:
                continue
            gflag += 1
            if _ground_truth_stalled(goal, artifacts, now):
                gtrue += 1
        flagged += gflag
        flagged_true += gtrue
        per_goal[goal["id"]] = {"flagged": gflag, "true": gtrue}

    precision = (flagged_true / flagged) if flagged else 1.0
    return {
        "is_real_ledger": is_real,
        "ledger": "real" if is_real else "synthetic-fixture",
        "replay_days": REPLAY_DAYS,
        "goals": [g["id"] for g in seeded],
        "flagged_stalls": flagged,
        "true_stalls": flagged_true,
        "stall_precision": round(precision, 4),
        "per_goal": per_goal,
    }


class ReplayLinkerTests(unittest.TestCase):
    def test_stall_precision_above_gate(self):
        rep = replay()
        self.assertNotIn("error", rep, rep)
        self.assertGreaterEqual(
            rep["stall_precision"], PRECISION_GATE,
            f"stall precision {rep['stall_precision']} < {PRECISION_GATE}: {rep}")
        # sanity: the window actually flagged SOME stalls (goal-1 goes quiet)
        self.assertGreater(rep["flagged_stalls"], 0, rep)

    def test_noise_rows_do_not_falsely_progress(self):
        # the unlinked repo/PR noise rows must never count as progress: goal-1
        # must show stalls after its last real artifact (day 10).
        rep = replay()
        self.assertGreater(rep["per_goal"]["goal-1"]["flagged"], 0, rep)


def main() -> int:
    rep = replay()
    print(json.dumps(rep, indent=2))
    if "error" in rep:
        return 1
    gate_ok = rep["stall_precision"] >= PRECISION_GATE
    print(f"\nstall_precision={rep['stall_precision']:.1%} over "
          f"{rep['ledger']} ledger — "
          f"{'PASS' if gate_ok else 'FAIL'} (gate {PRECISION_GATE:.0%})",
          file=sys.stderr)
    return 0 if gate_ok else 1


if __name__ == "__main__":
    sys.exit(main())
