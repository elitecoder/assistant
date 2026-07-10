#!/usr/bin/env python3
"""replay-865.py — PERMANENT regression fixture for the June incident
(Keel M3, design section 8).

In June the fleet fired the same "needs input" notification 865 times in one
day — one workspace signal re-surfacing every pulse with no dedup and no
budget. This eval replays that day as 865 requests for the SAME key against
bin/interrupt-gate.py with the notify budget raised to 1 (a full point MORE
generous than the shipped default of 0) and asserts the gate's contract:

    at most 1 delivered · 864 ledgered ``interrupt:denied`` rows

The first request spends the day's budget; every subsequent one is refused
by the 24h same-key dedup before the budget is even consulted. At the
shipped default budget (notify: 0) the same replay delivers ZERO — asserted
here too. Either way, all 865 verdicts are auditable: nothing is silently
swallowed.

Runnable standalone (python3 evals/noise/replay-865.py) AND as a unittest —
tests/test_noise_replay.py loads this module so `python3 -m unittest
discover tests` keeps the incident pinned forever.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]
GATE_PATH = REPO / "bin" / "interrupt-gate.py"

N_REQUESTS = 865
INCIDENT_KEY = "cmux:workspace:12:needs_input"
# 2026-06-14 08:00 LOCAL — one working day, mid-morning start, so all 865
# requests land on the same local date on every machine tz.
DAY_START = datetime(2026, 6, 14, 8, 0).timestamp()


def load_gate():
    name = "interrupt_gate_replay865"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(GATE_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Replay865(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.delivered = []

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _replay(self):
        """865 gate requests for the incident key, spread over one working
        day (~37s apart). Returns (n_delivered, n_denied)."""
        n_delivered = n_denied = 0
        for i in range(N_REQUESTS):
            res = self.gate.request(
                "notify", INCIDENT_KEY,
                "workspace:12 needs_input (Notification)",
                "approve tool use?",
                now=DAY_START + i * 37,
                deliver=lambda t, d: self.delivered.append(t))
            if res["delivered"]:
                n_delivered += 1
            else:
                n_denied += 1
        return n_delivered, n_denied

    def _ledger_counts(self):
        rows = [json.loads(l) for l in
                self.gate.ledger_path().read_text().splitlines()]
        return (sum(1 for r in rows if r["kind"] == "interrupt-delivered"),
                sum(1 for r in rows if r["kind"] == "interrupt-denied"))

    def test_replay_at_budget_one_delivers_at_most_once(self):
        p = self.gate.budget_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "date": self.gate.local_date(DAY_START),
            "budget": {"page": 0, "notify": 1},
        }))
        n_delivered, n_denied = self._replay()
        self.assertLessEqual(n_delivered, 1)
        self.assertEqual(n_delivered, 1)
        self.assertEqual(n_denied, 864)
        self.assertEqual(len(self.delivered), 1)
        led_delivered, led_denied = self._ledger_counts()
        self.assertEqual(led_delivered, 1)
        self.assertEqual(led_denied, 864)  # every denial is auditable

    def test_replay_at_default_budget_delivers_zero(self):
        n_delivered, n_denied = self._replay()  # no budget file → default 0/0
        self.assertEqual(n_delivered, 0)
        self.assertEqual(n_denied, N_REQUESTS)
        self.assertEqual(self.delivered, [])
        led_delivered, led_denied = self._ledger_counts()
        self.assertEqual(led_delivered, 0)
        self.assertEqual(led_denied, N_REQUESTS)


if __name__ == "__main__":
    unittest.main()
