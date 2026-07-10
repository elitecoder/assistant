"""Suite wrapper for the M4 stall-precision+RECALL harness
(evals/goals/replay-linker.py), Keel M2 fix. The old harness was circular
(oracle reused the code under test) and precision-only; this asserts the
rewritten harness's genuinely-independent, hand-authored ground truth gives
precision AND recall above their gates, and that a seeded OVER-matching linker
bug drops recall below gate (which the old precision-only harness could not
see).

Named test_replay_linker (sorts AFTER test_daemon); stdlib-only so it loads
under both python3.9 and python3.12.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _load_linker():
    spec = importlib.util.spec_from_file_location(
        "replay_linker_under_test", str(REPO / "evals" / "goals" / "replay-linker.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReplayLinkerHarnessTests(unittest.TestCase):
    def setUp(self):
        self.rl = _load_linker()

    def test_correct_linker_hits_precision_and_recall_gates(self):
        rep = self.rl.replay()
        self.assertGreaterEqual(rep["stall_precision"], self.rl.PRECISION_GATE, rep)
        self.assertGreaterEqual(rep["stall_recall"], self.rl.RECALL_GATE, rep)
        self.assertEqual(rep["false_positives"], 0, rep)
        self.assertEqual(rep["false_negatives"], 0, rep)
        self.assertGreater(rep["true_positives"], 0, rep)

    def test_over_matching_bug_drops_recall_below_gate(self):
        # The regression the old precision-only harness was blind to: an
        # over-matching linker never stalls → misses real stalls → recall dies.
        rep = self.rl.replay(match_override=lambda *a, **k: True)
        self.assertLess(rep["stall_recall"], self.rl.RECALL_GATE, rep)
        self.assertGreater(rep["false_negatives"], 0, rep)


if __name__ == "__main__":
    unittest.main()
