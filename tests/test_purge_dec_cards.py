"""Tests for the `dec-*` card predicate in bin/purge-stale-awaiting.py
(Keel M2): a card keyed by a decision id exists IFF that decision is still
`open` in the decision queue — open → keep, resolved/expired/unknown → drop,
unreadable stores → keep (fail-safe).

Version gate: purge-stale-awaiting.py has used PEP 604 (`str | None`) runtime
annotations since before M2, so importing/executing it under Python 3.9
crashes at module exec — that is the pre-existing baseline (the whole
test_purge_stale_awaiting.py + test_pickers_in_process purge suites FAIL/ERROR
under 3.9). These tests therefore skip below 3.10 rather than double-count
that known breakage; under 3.10+ they run in-process.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent

DEC_OPEN = "dec-" + "a" * 16
DEC_DONE = "dec-" + "b" * 16
DEC_GONE = "dec-" + "c" * 16


def _load_purge(home: Path):
    """Import the hyphenated script with HOME pointed at `home` (its path
    constants bind at import) — same pattern as test_pickers_in_process."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(
        "_purge_dec_cards", str(REPO / "bin" / "purge-stale-awaiting.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipIf(sys.version_info < (3, 10),
                 "purge-stale-awaiting.py uses PEP 604 runtime annotations "
                 "(pre-existing) — its in-process tests already ERROR on 3.9")
class DecCardPredicateTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        self.mod = _load_purge(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def write_queue(self, decisions_list):
        p = self.home / ".assistant/decisions/queue.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"schema": "decision-queue/1",
                                 "ts": "2026-07-09T12:00:00Z",
                                 "decisions": decisions_list}))
        return p

    def card(self, key):
        return {"key": key, "tier": "T2", "title": "t", "detail": "d"}

    STATUSES = {DEC_OPEN: [{"id": DEC_OPEN, "status": "open"},
                           {"id": DEC_DONE, "status": "accepted"}]}

    def test_open_decision_keeps_card(self):
        self.write_queue(self.STATUSES[DEC_OPEN])
        statuses = self.mod.load_decision_statuses()
        self.assertIsNone(self.mod.card_should_drop(
            self.card(DEC_OPEN), {"workspace:1"}, {}, statuses))

    def test_resolved_decision_drops_card(self):
        self.write_queue(self.STATUSES[DEC_OPEN])
        statuses = self.mod.load_decision_statuses()
        reason = self.mod.card_should_drop(
            self.card(DEC_DONE), {"workspace:1"}, {}, statuses)
        self.assertIn("accepted", reason)

    def test_unknown_decision_drops_card(self):
        self.write_queue(self.STATUSES[DEC_OPEN])
        statuses = self.mod.load_decision_statuses()
        reason = self.mod.card_should_drop(
            self.card(DEC_GONE), {"workspace:1"}, {}, statuses)
        self.assertIn("unknown", reason)

    def test_open_dec_card_ignores_every_other_predicate(self):
        # A dec card naming a CLOSED workspace still stays while the decision
        # is open — for dec-* keys the queue is the sole authority.
        self.write_queue([{"id": DEC_OPEN, "status": "open"}])
        statuses = self.mod.load_decision_statuses()
        card = {"key": DEC_OPEN, "tier": "T2", "title": "t",
                "detail": "workspace:99 needs you"}
        self.assertIsNone(self.mod.card_should_drop(
            card, {"workspace:1"}, {}, statuses))

    def test_unreadable_queue_keeps_cards_fail_safe(self):
        p = self.write_queue([])
        p.write_text("{torn")
        self.assertIsNone(self.mod.load_decision_statuses())
        self.assertIsNone(self.mod.card_should_drop(
            self.card(DEC_OPEN), {"workspace:1"}, {}, None))

    def test_missing_queue_falls_back_to_decisions_log(self):
        p = self.home / ".assistant/decisions/decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            f.write(json.dumps({"id": DEC_OPEN, "status": "open"}) + "\n")
            f.write(json.dumps({"id": DEC_OPEN, "status": "rejected"}) + "\n")
        statuses = self.mod.load_decision_statuses()
        self.assertEqual(statuses, {DEC_OPEN: "rejected"})  # last record wins

    def test_no_stores_at_all_drops_dec_cards(self):
        statuses = self.mod.load_decision_statuses()
        self.assertEqual(statuses, {})
        self.assertIsNotNone(self.mod.card_should_drop(
            self.card(DEC_OPEN), {"workspace:1"}, {}, statuses))

    def test_non_dec_cards_unaffected_by_dec_predicate(self):
        self.write_queue([])
        statuses = self.mod.load_decision_statuses()
        card = {"key": "workspace:4:needs_user", "tier": "T2",
                "title": "t", "detail": "d"}
        self.assertIsNone(self.mod.card_should_drop(
            card, {"workspace:4"}, {}, statuses))

    def test_main_purges_resolved_dec_card_end_to_end(self):
        self.write_queue([{"id": DEC_OPEN, "status": "open"},
                          {"id": DEC_DONE, "status": "expired"}])
        state_path = self.home / ".claude/cache/assistant-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"awaiting_input": [
            self.card(DEC_OPEN), self.card(DEC_DONE)]}))
        mod = _load_purge(self.home)  # rebind path constants to this HOME
        from unittest import mock
        with mock.patch.object(mod, "get_open_workspaces",
                               return_value={"workspace:1"}):
            with mock.patch.object(sys, "argv", ["purge-stale-awaiting.py"]):
                mod.main()
        kept = json.loads(state_path.read_text())["awaiting_input"]
        self.assertEqual([c["key"] for c in kept], [DEC_OPEN])


if __name__ == "__main__":
    unittest.main()
