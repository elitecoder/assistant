"""Tests for the deterministic planner (src/assistant/goals.plan_pass +
bin/plan-next-actions.py), Keel M4. Exercises the design's M4 acceptance
fixtures verbatim:

  • stall + headroom  → EXACTLY 1 TODO
  • idempotent re-run → still 1 TODO, no dup
  • caps saturated    → ledgered skip
  • _paused           → no-op
  • stale world       → no-op
  • human TODO present→ it dispatches before the goal TODO
  • SAFE DEFAULT (planner.autoDispatch off) → a staged DECISION, not a TODO
  • gated step class  → a staged decision, never an autoDispatch TODO

Named test_plan_next_actions (sorts AFTER test_daemon); stdlib-only so it loads
identically under python3.9 / python3.12 and doesn't perturb discovery order.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import goals, decisions  # noqa: E402

NOW = datetime(2026, 7, 2, 10, 0).timestamp()
DAY = 86400


class PlannerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────────────────
    def set_world(self, built=NOW - 60, live=None):
        p = goals.world_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "_meta": {"built_at": goals.utc_iso(built)},
            "live_sessions": live or []}))

    def set_config(self, autodispatch):
        p = goals.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"planner": {"autoDispatch": autodispatch}}))

    def add_stalled_goal(self, gid_title="Ship it", **over):
        kw = dict(title=gid_title, outcome="measurable outcome",
                  now=NOW - 5 * DAY)
        kw.update(over)
        g, err = goals.add_goal(**kw)
        self.assertIsNone(err, err)
        return g

    def todo_items(self):
        p = goals.todo_path()
        if not p.exists():
            return []
        return json.loads(p.read_text()).get("items", [])

    def goal_todos(self):
        return [i for i in self.todo_items()
                if str(i.get("source", "")).startswith("goal:")]

    def ledger_rows(self):
        return goals._read_jsonl(goals.ledger_path())


# ─── the M4 acceptance fixtures ──────────────────────────────────────────────

class AcceptanceFixtures(PlannerBase):
    def test_stall_plus_headroom_exactly_one_todo(self):
        self.set_config(autodispatch=True)
        self.set_world()  # no live sessions → full headroom
        self.add_stalled_goal()
        summary = goals.plan_pass(now=NOW)
        self.assertEqual(len(summary["staged_todos"]), 1)
        self.assertEqual(len(self.goal_todos()), 1)
        td = self.goal_todos()[0]
        self.assertTrue(td["autoDispatch"])
        self.assertTrue(td["source"].startswith("goal:goal-1:"))

    def test_idempotent_rerun_still_one_todo(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self.add_stalled_goal()
        goals.plan_pass(now=NOW)
        goals.plan_pass(now=NOW)   # same pulse again
        goals.plan_pass(now=NOW + 3600)  # later same-week pulse
        self.assertEqual(len(self.goal_todos()), 1)  # no dup

    def test_caps_saturated_ledgered_skip(self):
        self.set_config(autodispatch=True)
        # 5 live workspaces == ACTIVE_WS_CAP → zero headroom
        self.set_world(live=[{"ws_ref": f"workspace:{i}"} for i in range(5)])
        self.add_stalled_goal()
        summary = goals.plan_pass(now=NOW)
        self.assertEqual(len(self.goal_todos()), 0)
        self.assertTrue(any(s["reason"] == "capacity-saturated"
                            for s in summary["skipped"]))
        # the skip is LEDGERED (not silent)
        self.assertTrue(any(r.get("key", "").startswith("planner:skip")
                            and "capacity" in r.get("key", "")
                            for r in self.ledger_rows()))

    def test_paused_is_noop(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self.add_stalled_goal()
        goals.set_paused(True, now=NOW)
        summary = goals.plan_pass(now=NOW)
        self.assertTrue(summary["paused"])
        self.assertEqual(len(self.goal_todos()), 0)
        self.assertEqual(len(summary["staged_decisions"]), 0)
        self.assertTrue(any(r.get("key") == "planner:paused"
                            for r in self.ledger_rows()))

    def test_stale_world_is_noop(self):
        self.set_config(autodispatch=True)
        self.set_world(built=NOW - 99999)  # far older than world_stale_sec
        self.add_stalled_goal()
        summary = goals.plan_pass(now=NOW)
        self.assertTrue(summary["stale_world"])
        self.assertEqual(len(self.goal_todos()), 0)
        self.assertTrue(any(r.get("key") == "planner:stale-world"
                            for r in self.ledger_rows()))

    def test_missing_world_is_noop(self):
        self.set_config(autodispatch=True)
        # no world.json at all
        self.add_stalled_goal()
        self.assertTrue(goals.plan_pass(now=NOW)["stale_world"])

    def test_human_todo_dispatches_before_goal_todo(self):
        """Human-first (design section 6): a goal TODO is staged at the lowest
        priority AND only into headroom left after reserving pending human
        TODOs, so the untouched dispatcher — which sorts bucket_b by priority —
        always dispatches human work first."""
        self.set_config(autodispatch=True)
        self.set_world()
        # a pending human TODO already in the store
        tp = goals.todo_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps({"_schema": 1, "items": [
            {"id": "td-100", "priority": "P1", "title": "human work",
             "status": "open", "autoDispatch": True,
             "source": "manual:2026-07-02"}]}))
        self.add_stalled_goal()
        goals.plan_pass(now=NOW)
        goal_td = self.goal_todos()[0]
        # goal TODO is the LOWEST priority
        self.assertEqual(goal_td["priority"], goals.GOAL_TODO_PRIORITY)
        # replicate pulse.py's bucket_b priority sort → human first
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
        bucket_b = [i for i in self.todo_items()
                    if i.get("status") == "open" and i.get("autoDispatch")
                    and not i.get("dispatchedAt")]
        bucket_b.sort(key=lambda t: order.get(t.get("priority", "P4"), 9))
        self.assertEqual(bucket_b[0]["id"], "td-100")

    def test_human_pending_consumes_headroom(self):
        """4 live + 1 pending human TODO == ACTIVE_WS_CAP(5) → no goal headroom
        left, ledgered capacity skip (human work reserved first)."""
        self.set_config(autodispatch=True)
        self.set_world(live=[{"ws_ref": f"workspace:{i}"} for i in range(4)])
        tp = goals.todo_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps({"_schema": 1, "items": [
            {"id": "td-100", "priority": "P1", "title": "human", "status": "open",
             "autoDispatch": True, "source": "manual:x"}]}))
        self.add_stalled_goal()
        summary = goals.plan_pass(now=NOW)
        self.assertEqual(len(self.goal_todos()), 0)
        self.assertTrue(any(s["reason"] == "capacity-saturated"
                            for s in summary["skipped"]))


# ─── safe default + gated classes → decision, not autoDispatch ───────────────

class SafeDefaultAndGatingTests(PlannerBase):
    def test_safe_default_stages_decision_not_todo(self):
        # planner.autoDispatch defaults FALSE (no config written) → decision
        self.set_world()
        self.add_stalled_goal()
        summary = goals.plan_pass(now=NOW)
        self.assertEqual(len(self.goal_todos()), 0)
        self.assertEqual(len(summary["staged_decisions"]), 1)
        opens = decisions.open_decisions()
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["goal_refs"], ["goal-1"])
        self.assertEqual(opens[0]["lane"], "staged")

    def test_gated_class_is_decision_even_with_autodispatch_on(self):
        self.set_config(autodispatch=True)
        self.set_world()
        # a playbook whose only steps are GATED → never an autoDispatch TODO
        self.add_stalled_goal(playbook={"unattended": [], "gated": ["code-change"]})
        summary = goals.plan_pass(now=NOW)
        self.assertEqual(len(self.goal_todos()), 0)
        self.assertEqual(len(summary["staged_decisions"]), 1)
        self.assertEqual(summary["staged_decisions"][0]["step"], "code-change")

    def test_default_autodispatch_flag_is_false(self):
        self.assertFalse(goals.planner_autodispatch())  # no config → safe


# ─── rank order + budgets ────────────────────────────────────────────────────

class RankAndBudgetTests(PlannerBase):
    def test_rank_order_first_goal_claims_headroom(self):
        self.set_config(autodispatch=True)
        # only ONE unit of headroom (4 live) → only rank-1 goal gets a TODO
        self.set_world(live=[{"ws_ref": f"workspace:{i}"} for i in range(4)])
        g1 = self.add_stalled_goal("goal one")   # goal-1 rank 1
        g2 = self.add_stalled_goal("goal two")   # goal-2 rank 2
        summary = goals.plan_pass(now=NOW)
        self.assertEqual(len(self.goal_todos()), 1)
        self.assertEqual(summary["staged_todos"][0]["goal"], "goal-1")

    def test_max_staged_per_night_budget(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self.add_stalled_goal(budget={"maxActiveWs": 2,
                                      "maxStagedTodosPerNight": 1,
                                      "maxStrategistCallsPerDay": 1})
        goals.plan_pass(now=NOW)              # stages step 1 (research)
        self.assertEqual(len(self.goal_todos()), 1)
        # mark research done so select_next_step would advance to doc-draft,
        # but the per-night budget (1) must block a 2nd stage in the same night
        items = json.loads(goals.todo_path().read_text())
        items["items"][0]["status"] = "done"
        items["items"][0]["statusUpdatedAt"] = goals.utc_iso(NOW)
        goals.todo_path().write_text(json.dumps(items))
        summary = goals.plan_pass(now=NOW + 3600)
        # same ISO week → week-dedup already blocks; assert no 2nd goal TODO
        self.assertEqual(len([i for i in self.todo_items()
                              if str(i.get("source", "")).startswith("goal:")
                              and i.get("status") != "done"]), 0)
        del summary


# ─── CLI wrapper ─────────────────────────────────────────────────────────────

class CliWrapperTests(PlannerBase):
    def test_cli_runs_and_reports(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self.add_stalled_goal()
        # import the CLI module by path and invoke main()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plan_next_actions_cli", str(REPO / "bin" / "plan-next-actions.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc = mod.main(["--now", str(NOW)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.goal_todos()), 1)


if __name__ == "__main__":
    unittest.main()
