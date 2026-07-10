#!/usr/bin/env python3
"""replay-linker.py — stall PRECISION *and RECALL* harness for the mechanical
progress linker + stall detector (Keel M4 exit criterion: ">80% stall precision
hand-verified before autoDispatch defaults on").

M2 fix — the old harness was circular and precision-only:
  * it built ONE artifact list with the code under test and fed it to BOTH the
    detector AND the "oracle", so a linker extraction bug was mirrored into the
    oracle instead of caught;
  * the "oracle" re-derived matching from goals.* internals (not independent);
  * it never called goals.is_stalled — it inlined the stall math;
  * it reported PRECISION only, blind to RECALL (missed real stalls).

This version:
  * reads a genuinely INDEPENDENT, HAND-AUTHORED ground truth
    (fixtures/expected-stalls.json: a human's list of each goal's genuine
    progress days) and derives the truly-stalled day set from the mechanical
    stall formula WITHOUT any goals.* extraction;
  * drives the REAL detector: goals.last_progress_at over the extracted
    artifacts → goals.is_stalled(goal, now);
  * reports BOTH precision and recall, each with its own gate.

An over-matching linker (treats noise as progress) sees false progress every
day → never stalls → RECALL collapses; an under-matching one (stops matching
merged PRs) stalls too often → PRECISION drops. The correct linker matches the
hand-authored truth exactly (precision = recall = 1.0).

Ledger source: this environment has no real 30-day actions ledger, so the
harness runs against the committed SYNTHETIC fixture the ground truth describes.

    python3 evals/goals/replay-linker.py        # prints the precision+recall report
    python3 -m unittest evals.goals.replay-linker
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import goals  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
DAY = 86400
PRECISION_GATE = 0.80
RECALL_GATE = 0.80


def _load_truth() -> dict:
    return json.loads((FIX / "expected-stalls.json").read_text())


def _load_goals() -> list[dict]:
    store = json.loads((FIX / "seeded-goals.json").read_text())
    return [goals._normalize_goal(g) for g in store["goals"]
            if goals._valid_goal(g)]


def _oracle_stalled(genuine_days: list[int], stall_after_h: float,
                    n: int) -> bool:
    """INDEPENDENT oracle: truly stalled at day N iff no HAND-AUTHORED genuine
    progress day within the trailing stall window. Pure arithmetic over the
    hand-authored day list — no goals.* extraction touches this."""
    window_days = stall_after_h / 24.0
    last = max((g for g in genuine_days if g <= n), default=None)
    if last is None:
        return False
    return (n - last) > window_days


def replay(match_override=None) -> dict:
    """Walk the replay window; per (goal, day) compare the REAL detector against
    the independent oracle. `match_override` (a fn replacing goals._matches)
    seeds a linker bug to prove the gates bite. Runs under a throwaway $HOME so
    goals.is_stalled's open-decision check sees an empty store."""
    truth = _load_truth()
    days = int(truth["replay_days"])
    stall_after_h = float(truth["stall_after_hours"])
    genuine = truth["genuine_progress_days"]
    seeded = _load_goals()

    ledger_rows = goals._read_jsonl(FIX / "ledger-30d.jsonl")
    prs = goals._merged_prs_from_ledger(ledger_rows)
    artifacts = goals.ledger_artifacts(ledger_rows) + goals.pr_artifacts(prs)

    # Anchor day indices to the artifact epochs (the timeline the detector
    # actually sees). Day-0 is the first ledger row; genuine_progress_days are
    # integer offsets from it. (The fixture's `epoch` fields, not its `ts`
    # strings, are the detector's clock.)
    epochs = [a["ts"] for a in artifacts if isinstance(a.get("ts"), (int, float))]
    base = min(epochs)

    orig_matches = goals._matches
    old_home = os.environ.get("HOME")
    tmp = tempfile.mkdtemp()
    os.environ["HOME"] = tmp
    if match_override is not None:
        goals._matches = match_override
    try:
        tp = fp = fn = 0
        per_goal: dict[str, dict] = {}
        for goal in seeded:
            gid = goal["id"]
            g_tp = g_fp = g_fn = 0
            gdays = genuine.get(gid, [])
            for n in range(1, days + 1):
                now = base + n * DAY
                lp = goals.last_progress_at(goal, artifacts, now=now)
                probe = dict(goal)
                probe["lastProgressAt"] = goals.utc_iso(lp) if lp is not None else None
                det = goals.is_stalled(probe, now=now)          # the REAL detector
                oracle = _oracle_stalled(gdays, stall_after_h, n)
                if det and oracle:
                    g_tp += 1
                elif det and not oracle:
                    g_fp += 1
                elif oracle and not det:
                    g_fn += 1
            tp += g_tp
            fp += g_fp
            fn += g_fn
            per_goal[gid] = {"tp": g_tp, "fp": g_fp, "fn": g_fn}
    finally:
        goals._matches = orig_matches
        if old_home is not None:
            os.environ["HOME"] = old_home
        else:  # pragma: no cover
            os.environ.pop("HOME", None)

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {
        "ledger": "synthetic-fixture",
        "replay_days": days,
        "goals": [g["id"] for g in seeded],
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "stall_precision": round(precision, 4),
        "stall_recall": round(recall, 4),
        "per_goal": per_goal,
    }


class ReplayLinkerTests(unittest.TestCase):
    def test_precision_and_recall_above_gates(self):
        rep = replay()
        self.assertGreaterEqual(rep["stall_precision"], PRECISION_GATE, rep)
        self.assertGreaterEqual(rep["stall_recall"], RECALL_GATE, rep)
        # the correct linker matches the hand-authored truth EXACTLY
        self.assertEqual(rep["false_positives"], 0, rep)
        self.assertEqual(rep["false_negatives"], 0, rep)
        # and it actually flagged real stalls (the window isn't trivially empty)
        self.assertGreater(rep["true_positives"], 0, rep)

    def test_over_matching_bug_drops_recall_below_gate(self):
        # Seed an OVER-matching linker: every artifact matches every goal, so
        # last_progress_at returns a recent (noise) ts every day → the detector
        # never stalls → it MISSES the real stalls → recall collapses. The old
        # precision-only harness was blind to exactly this.
        rep = replay(match_override=lambda *a, **k: True)
        self.assertLess(rep["stall_recall"], RECALL_GATE, rep)
        self.assertGreater(rep["false_negatives"], 0, rep)


def main() -> int:
    rep = replay()
    print(json.dumps(rep, indent=2))
    ok = (rep["stall_precision"] >= PRECISION_GATE
          and rep["stall_recall"] >= RECALL_GATE)
    print(f"\nprecision={rep['stall_precision']:.1%} recall={rep['stall_recall']:.1%} "
          f"— {'PASS' if ok else 'FAIL'} (gates P{PRECISION_GATE:.0%}/R{RECALL_GATE:.0%})",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
