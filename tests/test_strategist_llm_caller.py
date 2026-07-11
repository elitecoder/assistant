"""Tests for bin/strategist.py's LLM subprocess caller (Keel M6): the Observer
subprocess pattern verbatim — file-side-effect output (draft.json / context.md),
archived runs under ~/.assistant/strategist-runs/, usage wrapped in metering
(cost-ledger row, caller="strategist"), tolerant parse. The subprocess itself is
simulated by stubbing strategist_bin.run — NO LLM, NO network (this is the first
new LLM caller since M2, so the caller MUST be exercisable with zero live spend).

Mirrors tests/test_triage_llm_caller.py's structure/assertions so the two new
LLM callers stay on the same governance rails.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
STRAT_PATH = REPO / "bin/strategist.py"


def load_strategist_bin(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("strategist_bin_mod",
                                                  str(STRAT_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOAL = {"id": "goal-1", "title": "Ship X", "outcome": "X done",
        "links": {}, "playbook": {"unattended": ["research"], "gated": []}}


class StrategistDraftCallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        self.mod = load_strategist_bin(self._tmp)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _fake_run(self, draft_obj):
        """Stub for strategist_bin.run: writes draft.json (path found in the
        prompt) and returns a usage-bearing CLI envelope on stdout."""
        def fake(cmd, *, input_text=None, timeout=30, merge_bedrock=False):
            m = re.search(r"^\s+(/\S+/draft\.json)\s*$", input_text or "",
                          re.MULTILINE)
            if m and draft_obj is not None:
                Path(m.group(1)).write_text(json.dumps(draft_obj))
            envelope = {"usage": {"input_tokens": 400, "output_tokens": 30},
                        "total_cost_usd": 0.0015}
            return 0, json.dumps(envelope), ""
        return fake

    def _cost_rows(self):
        p = self._tmp / ".assistant/cost-ledger.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def test_archives_run_and_parses_draft(self):
        draft = {"step_class": "research", "title": "DRAFT T", "detail": "DRAFT D"}
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run(draft)):
            out = self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 7)
        self.assertEqual(out, draft)
        run_dir = self._tmp / ".assistant/strategist-runs/0007/draft-goal-1-research"
        for name in ("prompt.md", "stdout.txt", "stderr.txt", "meta.json",
                     "draft.json"):
            self.assertTrue((run_dir / name).exists(), msg=name)
        meta = json.loads((run_dir / "meta.json").read_text())
        self.assertEqual(meta["usage"]["source"], "cli")

    def test_cost_ledger_row_caller_strategist(self):
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run(
                                   {"title": "a", "detail": "bb"})):
            self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 1)
        rows = self._cost_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["caller"], "strategist")
        self.assertEqual(rows[0]["tokens_in"], 400)
        self.assertEqual(rows[0]["est_usd"], 0.0015)
        self.assertEqual(rows[0]["status"], "ok")

    def test_subprocess_failure_returns_none_and_estimated_failed_row(self):
        # C-F-2/C-O-3: a 240s timeout (rc=124) was almost certainly billed
        # server-side. The row is still status="failed" (stays visible) but now
        # books an ESTIMATED, NON-ZERO cost (chars/4 over the prompt we sent) so
        # repeated failures RATCHET the daily ceiling instead of evading it.
        with mock.patch.object(self.mod, "run",
                               return_value=(124, "", "timeout after 240s")):
            out = self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 1)
        self.assertIsNone(out)
        rows = self._cost_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertGreater(rows[0]["est_usd"], 0.0)  # NOT a phantom $0 anymore
        self.assertGreater(rows[0]["tokens_in"], 0)

    def test_unparseable_envelope_books_estimated_failed_row(self):
        with mock.patch.object(self.mod, "run",
                               return_value=(0, "no json here", "")):
            self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 1)
        rows = self._cost_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertGreater(rows[0]["est_usd"], 0.0)

    def test_repeated_failures_ratchet_the_daily_ceiling(self):
        # The whole point of C-F-2: failures must count toward the ceiling.
        # Point the pure module's day_spend at the same cost-ledger and prove
        # that N failed calls push day_spend over a low ceiling → over_ceiling.
        import time as _t
        strat = self.mod._load_strategist()
        # a low ceiling for this goal-less governance check
        cfgp = self._tmp / ".assistant" / "comms" / "config.json"
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"strategist": {"dailyCostCeilingUsd": 0.01}}))
        now = _t.time()
        self.assertFalse(strat.over_ceiling(now))
        with mock.patch.object(self.mod, "run",
                               return_value=(124, "", "timeout after 240s")):
            for _ in range(20):
                self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 1)
        self.assertGreater(strat.day_spend_usd(now), 0.0)
        self.assertTrue(strat.over_ceiling(now))  # failures tripped the ceiling

    def test_missing_draft_file_returns_none(self):
        # rc==0, envelope ok, but the model wrote nothing → None (→ template).
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run(None)):
            out = self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 1)
        self.assertIsNone(out)

    def test_missing_prompt_skips_the_call(self):
        with mock.patch.object(self.mod, "STRATEGIST_DRAFT_PROMPT",
                               self._tmp / "nope.md"):
            with mock.patch.object(self.mod, "run") as run_mock:
                self.assertIsNone(
                    self.mod.call_strategist_draft(GOAL, "research", "TT", "TD", 1))
            run_mock.assert_not_called()

    def test_draft_for_planner_gates_and_falls_back_to_template(self):
        # End-to-end through the pure module: a valid draft upgrades the text.
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run(
                                   {"step_class": "research",
                                    "title": "DRAFTED", "detail": "better text"})):
            import time
            out = self.mod.draft_for_planner(GOAL, "research", "TT", "TD",
                                             time.time(), pulse_idx=2)
        self.assertEqual(out, ("DRAFTED", "better text"))


class StrategistContextCallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        (self._tmp / ".claude" / "cache").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        self.mod = load_strategist_bin(self._tmp)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _fake_run(self, markdown):
        def fake(cmd, *, input_text=None, timeout=30, merge_bedrock=False):
            m = re.search(r"^\s+(/\S+/context\.md)\s*$", input_text or "",
                          re.MULTILINE)
            if m and markdown is not None:
                Path(m.group(1)).write_text(markdown)
            envelope = {"usage": {"input_tokens": 200, "output_tokens": 50},
                        "total_cost_usd": 0.001}
            return 0, json.dumps(envelope), ""
        return fake

    def test_context_caller_writes_and_meters(self):
        dec = {"id": "dec-1", "title": "Do the thing", "snippet": "ctx",
               "source": "github", "kind": "review_requested", "refs": {}}
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run("## Context\n\nresearch")):
            out = self.mod.call_strategist_context(dec, 3)
        self.assertEqual(out, "## Context\n\nresearch")
        rows = [json.loads(l) for l in
                (self._tmp / ".assistant/cost-ledger.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["caller"], "strategist")

    def test_pre_research_wraps_context_caller_on_idle(self):
        # Fresh world (idle) + one open decision → pre_research writes context.
        import time
        now = time.time()
        from datetime import datetime, timezone
        built = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        world = self._tmp / ".claude" / "cache" / "world.json"
        world.write_text(json.dumps({
            "_meta": {"built_at": built}, "live_sessions": []}))
        decdir = self._tmp / ".assistant" / "decisions"
        decdir.mkdir(parents=True)
        (decdir / "decisions.jsonl").write_text(json.dumps({
            "schema": "decision/1", "id": "dec-9", "epoch": int(now),
            "status": "open", "lane": "staged", "goal_refs": [],
            "title": "Review PR", "snippet": "please", "source": "github",
            "kind": "review_requested", "refs": {}}) + "\n")
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run("## prepared context")):
            summ = self.mod.pre_research(pulse_idx=1, now=now)
        self.assertEqual(summ["researched"], ["dec-9"])
        ctx = (self._tmp / ".assistant" / "decision-context" / "dec-9.md").read_text()
        self.assertIn("prepared context", ctx)


if __name__ == "__main__":
    unittest.main()
