"""Tests for bin/interrupt-gate.py (Keel M3): the gate matrix. Default
budget 0 → every request suppressed + ledgered; ladder requirements for
`page`; 24h same-key dedup (spanning midnight); budget-exhausted-mid-day and
date-rollover behavior (driven by the shape fixtures under
evals/noise/fixtures/); delivery-failure accounting.

The gate module is loaded via importlib (dashed filename, kept that way so
the no-rogue-notifications grep allowlist is one exact path). Delivery is
injected — no real push surface is ever touched by the suite.
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

REPO = Path(__file__).resolve().parent.parent
GATE_PATH = REPO / "bin" / "interrupt-gate.py"
FIXTURES = REPO / "evals" / "noise" / "fixtures"

# 2026-07-02 10:00 LOCAL — mid-day on every machine tz, so "same local
# date" fixtures behave identically everywhere.
NOW = datetime(2026, 7, 2, 10, 0).timestamp()


def load_gate():
    name = "interrupt_gate_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(GATE_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class GateBase(unittest.TestCase):
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

    def deliver(self, title, detail):
        self.delivered.append((title, detail))

    def request(self, level="notify", key="k1", title="t", detail="d",
                now=NOW, **kw):
        return self.gate.request(level, key, title, detail, now=now,
                                 deliver=self.deliver, **kw)

    def set_budget(self, doc):
        p = self.gate.budget_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))

    def ledger_rows(self, kind):
        p = self.gate.ledger_path()
        if not p.exists():
            return []
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        return [r for r in rows if r.get("kind") == kind]

    def load_fixture(self, name, **stamp):
        """Shape fixtures store structure; dates are stamped at load time so
        the suite is timezone-independent."""
        doc = json.loads((FIXTURES / name).read_text())
        doc.update(stamp)
        return doc


class DefaultSilentTests(GateBase):
    def test_default_budget_is_zero_and_denies(self):
        res = self.request()
        self.assertFalse(res["delivered"])
        self.assertIn("budget exhausted (0/0", res["reason"])
        self.assertEqual(self.delivered, [])
        # The denial is auditable: ledger row + suppressed tail + counter.
        denied = self.ledger_rows("interrupt-denied")
        self.assertEqual(len(denied), 1)
        self.assertTrue(denied[0]["key"].startswith("interrupt:denied:notify:"))
        self.assertEqual(denied[0]["outcome"], "skipped")
        doc = json.loads(self.gate.budget_path().read_text())
        self.assertEqual(doc["budget"], {"page": 0, "notify": 0})
        self.assertEqual(doc["denied_today"], 1)
        self.assertEqual(len(doc["suppressed_today"]), 1)
        self.assertEqual(doc["suppressed_today"][0]["key"], "k1")

    def test_unknown_level_denied_not_raised(self):
        res = self.request(level="klaxon")
        self.assertFalse(res["delivered"])
        self.assertIn("unknown level", res["reason"])

    def test_unreadable_budget_falls_back_silent(self):
        p = self.gate.budget_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{corrupt")
        res = self.request()
        self.assertFalse(res["delivered"])
        self.assertIn("budget exhausted", res["reason"])


class BudgetAndDedupTests(GateBase):
    def test_headroom_delivers_then_same_key_dedups(self):
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"notify": 5, "page": 0}})
        first = self.request(key="ws-crash:workspace:7")
        self.assertTrue(first["delivered"])
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(len(self.ledger_rows("interrupt-delivered")), 1)
        # Same key within 24h → denied despite budget headroom.
        second = self.request(key="ws-crash:workspace:7", now=NOW + 3600)
        self.assertFalse(second["delivered"])
        self.assertIn("same-key delivery within 24h", second["reason"])
        # A different key still goes through.
        third = self.request(key="ws-crash:workspace:8", now=NOW + 3600)
        self.assertTrue(third["delivered"])

    def test_dedup_spans_midnight(self):
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"notify": 5, "page": 0}})
        self.assertTrue(self.request(key="k")["delivered"])
        # 14h later = next local day, but still inside the 24h window.
        res = self.request(key="k", now=NOW + 14 * 3600)
        self.assertFalse(res["delivered"])
        self.assertIn("same-key", res["reason"])
        # 25h later the key is free again.
        res2 = self.request(key="k", now=NOW + 25 * 3600)
        self.assertTrue(res2["delivered"])

    def test_budget_exhausted_mid_day_fixture(self):
        """evals/noise fixture: used == budget with the date still today —
        the remaining day is silent, and auditable as such."""
        self.set_budget(self.load_fixture(
            "budget-exhausted-mid-day.json",
            date=self.gate.local_date(NOW)))
        res = self.request(key="fresh-key")
        self.assertFalse(res["delivered"])
        self.assertIn("budget exhausted (1/1", res["reason"])
        self.assertEqual(len(self.ledger_rows("interrupt-denied")), 1)

    def test_date_rollover_resets_used_counts(self):
        """evals/noise fixture: yesterday exhausted its budget; today's
        first request gets a fresh used-count (budget + ladder survive the
        rollover, suppressed tail and counters reset)."""
        yesterday = self.gate.local_date(NOW - 86400)
        self.set_budget(self.load_fixture(
            "date-rollover.json", date=yesterday,
            last_delivered={"old-key": NOW - 90000}))
        res = self.request(key="new-day-key")
        self.assertTrue(res["delivered"])
        doc = json.loads(self.gate.budget_path().read_text())
        self.assertEqual(doc["date"], self.gate.local_date(NOW))
        self.assertEqual(doc["used"], {"notify": 1})
        self.assertEqual(doc["denied_today"], 0)
        self.assertEqual(doc["suppressed_today"], [])
        # >24h-old dedup entries are pruned at rollover.
        self.assertNotIn("old-key", doc["last_delivered"])

    def test_booking_before_delivery(self):
        """A delivery failure still consumes the slot (crash-safe direction:
        under-notify, never over-notify) and is ledgered as a denial."""
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"notify": 1, "page": 0}})

        def boom(title, detail):
            raise RuntimeError("push surface down")

        res = self.gate.request("notify", "k", "t", now=NOW, deliver=boom)
        self.assertFalse(res["delivered"])
        self.assertIn("delivery failed", res["reason"])
        doc = json.loads(self.gate.budget_path().read_text())
        self.assertEqual(doc["used"], {"notify": 1})
        self.assertEqual(len(self.ledger_rows("interrupt-denied")), 1)


class DeliveryEscapeTests(GateBase):
    def test_osascript_escaping_is_injection_safe(self):
        """Moved here from the workspace-watcher suite when the push call
        moved into the gate: backslash escaped first, then double-quote, so
        no user text can break out of the AppleScript string."""
        from unittest import mock
        with mock.patch.object(self.gate.subprocess, "run") as run_mock:
            self.gate._deliver_osascript('ti"tle\\x', 'mes"sage\\y')
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        script = argv[2]
        self.assertIn('ti\\"tle\\\\x', script)
        self.assertIn('mes\\"sage\\\\y', script)


class LadderTests(GateBase):
    def test_page_requires_full_ladder(self):
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"notify": 0, "page": 5}})
        # Missing every requirement.
        res = self.request(level="page", key="p1")
        self.assertFalse(res["delivered"])
        self.assertIn("ladder", res["reason"])
        # Partially met is still denied.
        res2 = self.request(level="page", key="p2", lane="escalate",
                            urgency="now", pageable=False)
        self.assertFalse(res2["delivered"])
        self.assertIn("pageable", res2["reason"])
        # ALL of lane=escalate + urgency=now + pageable → delivered.
        res3 = self.request(level="page", key="p3", lane="escalate",
                            urgency="now", pageable=True)
        self.assertTrue(res3["delivered"])

    def test_page_ladder_survives_config_erasure(self):
        """A hand-edited ladder that drops the page rung does NOT open the
        page gate — the default requirements are enforced in code."""
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"page": 5}, "ladder": []})
        res = self.request(level="page", key="p1")
        self.assertFalse(res["delivered"])
        self.assertIn("ladder", res["reason"])

    def test_notify_has_no_ladder_by_default(self):
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"notify": 1, "page": 0}})
        self.assertTrue(self.request(key="n1")["delivered"])


# ─── adversarial-review regressions (Keel M3 fix cycle) ──────────────────────

class EmptyRequiresFallbackTests(GateBase):
    """F1: a present-but-empty {"requires": {}} page entry must fall back to
    the default page gate — config can tighten the ladder, never erase it."""

    def test_empty_requires_page_entry_still_gated(self):
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"page": 5},
                         "ladder": [{"level": "page", "requires": {}}]})
        # Wrong lane (default None) → the restored default gate denies it.
        res = self.request(level="page", key="p1")
        self.assertFalse(res["delivered"])
        self.assertIn("ladder", res["reason"])
        # A fully-qualified page still passes the restored gate.
        ok = self.request(level="page", key="p2", lane="escalate",
                          urgency="now", pageable=True)
        self.assertTrue(ok["delivered"])


class DateResetFailsQuietTests(GateBase):
    """F2: a missing / typo'd / unparseable date must KEEP the used counts
    (fail toward quieter), never hand back a fresh quota with a raised
    budget. Only a genuine, valid rollover resets."""

    def test_missing_date_keeps_used(self):
        self.set_budget({"budget": {"notify": 5}, "used": {"notify": 3}})
        doc = self.gate.load_budget(NOW)
        self.assertEqual(doc["used"].get("notify"), 3)

    def test_typo_date_keeps_used(self):
        self.set_budget({"date": "not-a-date", "budget": {"notify": 5},
                         "used": {"notify": 3}})
        doc = self.gate.load_budget(NOW)
        self.assertEqual(doc["used"].get("notify"), 3)

    def test_valid_older_date_resets_used(self):
        self.set_budget({"date": "2026-07-01", "budget": {"notify": 5},
                         "used": {"notify": 3}})
        doc = self.gate.load_budget(NOW)  # NOW is 2026-07-02 local
        self.assertEqual(doc["used"], {})


class LevelNamespacedDedupTests(GateBase):
    """F12: dedup is keyed by (level, key) — a delivered notify must not
    suppress a later PAGE on the same key for 24h."""

    def test_notify_does_not_suppress_later_page(self):
        self.set_budget({"date": self.gate.local_date(NOW),
                         "budget": {"page": 1, "notify": 1}})
        n = self.request(level="notify", key="k")
        self.assertTrue(n["delivered"])
        p = self.request(level="page", key="k", lane="escalate",
                         urgency="now", pageable=True)
        self.assertTrue(p["delivered"])


if __name__ == "__main__":
    unittest.main()
