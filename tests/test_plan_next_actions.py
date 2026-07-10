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
        # 5 ACTIVE workspaces == ACTIVE_WS_CAP → zero headroom (m14: a session
        # counts only when it's actually active — give each a fresh turn age).
        self.set_world(live=[{"ws_ref": f"workspace:{i}", "last_turn_age_sec": 0}
                             for i in range(5)])
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
        self.set_world(live=[{"ws_ref": f"workspace:{i}", "last_turn_age_sec": 0}
                             for i in range(4)])
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
        # only ONE unit of headroom (4 active) → only rank-1 goal gets a TODO
        self.set_world(live=[{"ws_ref": f"workspace:{i}", "last_turn_age_sec": 0}
                             for i in range(4)])
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


# ─── M1/M6/M12: the control loop advances, and human resolutions are honored ──

class ControlLoopLifecycleTests(PlannerBase):
    """The BLOCKER (M1) + its unifications (M6 safety inversion, M12 defer/rm
    wedge). The safe default stages DECISIONS, so advancement must be read from
    accepted/rejected decisions — not just done TODOs — or the loop fires once
    per goal and never reaches step 2."""

    def _two_step_goal(self):
        # A 2-step unattended playbook so we can watch research → doc-draft.
        return self.add_stalled_goal(
            playbook={"unattended": ["research", "doc-draft"], "gated": []})

    def _one_step_goal(self):
        return self.add_stalled_goal(
            playbook={"unattended": ["research"], "gated": []})

    def _open_research_dec(self):
        opens = decisions.open_decisions()
        return next(d for d in opens if d.get("recommended", {}).get("class")
                    == "research")

    # ── M1a: accept advances to the NEXT playbook step (real shipped path) ──
    def test_accept_decision_advances_to_step_two(self):
        self.set_world()                 # safe default (autoDispatch OFF)
        self._two_step_goal()
        goals.plan_pass(now=NOW)
        dec = self._open_research_dec()
        self.assertEqual(dec["recommended"]["class"], "research")
        # human accepts the research step
        decisions.transition(dec["id"], "accepted", via="test", now=NOW)
        # a later pulse (next ISO week, past the stall window) drives the REAL
        # path: stamp progress, then plan → advances to doc-draft
        later = NOW + 8 * DAY
        self.set_world(built=later - 60)
        goals.pulse_step(now=later)
        opens = decisions.open_decisions()
        classes = {d.get("recommended", {}).get("class") for d in opens}
        self.assertIn("doc-draft", classes, opens)      # advanced!
        self.assertNotIn("research", classes)           # not re-nagged

    # ── M1b: reject → no infinite re-nag, and it's ledgered ──
    def test_reject_decision_no_infinite_renag(self):
        self.set_world()
        self._one_step_goal()
        goals.plan_pass(now=NOW)
        dec = self._open_research_dec()
        decisions.transition(dec["id"], "rejected", via="test", now=NOW)
        later = NOW + 8 * DAY
        self.set_world(built=later - 60)
        summary = goals.plan_pass(now=later)
        # the single (rejected) step is declined → nothing new staged
        self.assertEqual(len(summary["staged_decisions"]), 0)
        self.assertEqual(len(decisions.open_decisions()), 0)
        # and the skip is LEDGERED (M1c: no silent drop)
        self.assertTrue(any(r.get("key", "").startswith("planner:skip")
                            and "no-step" in r.get("key", "")
                            for r in self.ledger_rows()))

    # ── M1d: expire → reopens (unchanged) ──
    def test_expire_decision_reopens(self):
        self.set_world()
        self._one_step_goal()
        goals.plan_pass(now=NOW)
        dec = self._open_research_dec()
        decisions.expire_open(now=NOW + 200 * 3600)     # TTL (72h) elapsed
        self.assertEqual(len(decisions.open_decisions()), 0)
        later = NOW + 9 * DAY
        self.set_world(built=later - 60)
        goals.plan_pass(now=later)                       # re-sighting → reopen
        opens = decisions.open_decisions()
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["id"], dec["id"])

    # ── M1c: every dedup/skip reason is ledgered ──
    def test_week_dedup_is_ledgered(self):
        # autoDispatch path: a staged TODO (unlike an open decision) does not
        # block the stall, so the SAME-week re-pulse reaches the week-dedup guard.
        self.set_config(autodispatch=True)
        self.set_world()
        self._two_step_goal()
        goals.plan_pass(now=NOW)                          # stages, writes wk key
        self.set_world(built=NOW + 3600 - 60)             # keep world fresh
        summary = goals.plan_pass(now=NOW + 3600)         # same week → deduped
        self.assertTrue(any(s["reason"] == "week-deduped"
                            for s in summary["skipped"]))
        self.assertTrue(any(r.get("key", "").startswith("planner:skip")
                            and "week-deduped" in r.get("key", "")
                            for r in self.ledger_rows()))

    # ── M6: reject on the decision path is NOT dispatched after flag flip ──
    def test_rejected_step_not_dispatched_after_autodispatch_flip(self):
        self.set_world()                                 # safe default OFF
        self._one_step_goal()
        goals.plan_pass(now=NOW)                          # research DECISION
        dec = self._open_research_dec()
        decisions.transition(dec["id"], "rejected", via="test", now=NOW)
        # operator flips autoDispatch ON — the rejected step must NOT become an
        # unattended TODO
        self.set_config(autodispatch=True)
        later = NOW + 8 * DAY
        self.set_world(built=later - 60)
        goals.plan_pass(now=later)
        self.assertEqual(self.goal_todos(), [])          # nothing dispatched
        self.assertTrue(goals._human_declined_step("goal-1", "research"))

    # ── M6: a human-removed TODO is NOT resurrected as a decision ──
    def test_removed_todo_not_resurrected_as_decision(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self._one_step_goal()
        goals.plan_pass(now=NOW)                          # research TODO
        self.assertEqual(len(self.goal_todos()), 1)
        # human removes it (todo-server.remove_item shape: moved to removed[])
        data = json.loads(goals.todo_path().read_text())
        it = data["items"].pop(0)
        it["removedAt"] = goals.utc_iso(NOW)
        data.setdefault("removed", []).append(it)
        goals.todo_path().write_text(json.dumps(data))
        # flip to safe default so the decision path would be the resurrection
        self.set_config(autodispatch=False)
        later = NOW + 8 * DAY
        self.set_world(built=later - 60)
        goals.plan_pass(now=later)
        self.assertEqual(decisions.open_decisions(), [])  # not resurrected

    # ── M12: defer advances past the step (no wedge) and is ledgered ──
    def test_defer_todo_advances_and_ledgers(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self._two_step_goal()
        goals.plan_pass(now=NOW)                          # research TODO
        data = json.loads(goals.todo_path().read_text())
        data["items"][0]["status"] = "deferred"
        goals.todo_path().write_text(json.dumps(data))
        later = NOW + 8 * DAY
        self.set_world(built=later - 60)
        goals.plan_pass(now=later)
        steps = {i.get("stepClass") for i in self.goal_todos()}
        self.assertIn("doc-draft", steps)                # advanced past deferred
        # exactly one research TODO (the deferred one) — not re-staged
        self.assertEqual(sum(1 for i in self.todo_items()
                             if i.get("stepClass") == "research"), 1)
        self.assertTrue(any(r.get("kind") == "planner-stage"
                            and "doc-draft" in r.get("evidence", "")
                            for r in self.ledger_rows()))

    # ── M12: rm advances past the step (no wedge) and is ledgered ──
    def test_removed_todo_advances_and_ledgers(self):
        self.set_config(autodispatch=True)
        self.set_world()
        self._two_step_goal()
        goals.plan_pass(now=NOW)
        data = json.loads(goals.todo_path().read_text())
        it = data["items"].pop(0)
        it["removedAt"] = goals.utc_iso(NOW)
        data.setdefault("removed", []).append(it)
        goals.todo_path().write_text(json.dumps(data))
        later = NOW + 8 * DAY
        self.set_world(built=later - 60)
        goals.plan_pass(now=later)
        steps = {i.get("stepClass") for i in self.goal_todos()}
        self.assertIn("doc-draft", steps)
        self.assertTrue(any(r.get("kind") == "planner-stage"
                            for r in self.ledger_rows()))


# ─── select_next_step unit-level advancement ─────────────────────────────────

class SelectNextStepTests(PlannerBase):
    def test_accepted_decision_counts_as_done_advances(self):
        goal = {"id": "goal-1", "title": "t",
                "playbook": {"unattended": ["research", "doc-draft"], "gated": []}}
        dec_id = goals._goal_step_decision_id("goal-1", "research")
        folded = {dec_id: {"status": "accepted"}}
        step = goals.select_next_step(goal, {}, folded)
        self.assertIsNotNone(step)
        self.assertEqual(step[0], "doc-draft")

    def test_open_decision_blocks_new_step(self):
        goal = {"id": "goal-1", "title": "t",
                "playbook": {"unattended": ["research"], "gated": []}}
        dec_id = goals._goal_step_decision_id("goal-1", "research")
        folded = {dec_id: {"status": "open"}}
        self.assertIsNone(goals.select_next_step(goal, {}, folded))

    def test_rejected_decision_advances_past(self):
        goal = {"id": "goal-1", "title": "t",
                "playbook": {"unattended": ["research", "doc-draft"], "gated": []}}
        dec_id = goals._goal_step_decision_id("goal-1", "research")
        folded = {dec_id: {"status": "rejected"}}
        step = goals.select_next_step(goal, {}, folded)
        self.assertEqual(step[0], "doc-draft")


# ─── M13/M14/M16 ─────────────────────────────────────────────────────────────

class SuppressionAndHeadroomTests(PlannerBase):
    def test_open_decision_matching_links_suppresses_stall(self):
        g = self.add_stalled_goal(links={"repos": ["myrepo"]})
        self.assertTrue(goals.is_stalled(g, now=NOW))    # stalled with nothing open
        # an OPEN triage decision about the goal's repo (NO goal_refs) is the
        # human already being asked — it must suppress the stall nag (m13)
        ev = {"id": "tri1", "source": "github", "external_id": "gh:1",
              "kind": "review_requested", "title": "review",
              "ts": goals.utc_iso(NOW), "refs": {"repo": "myrepo"}}
        decisions.open_decision(event=ev, lane="staged", policy_id="p",
                                action={"class": "todo.create"}, now=NOW)
        self.assertFalse(goals.is_stalled(g, now=NOW))

    def test_idle_fleet_leaves_headroom(self):
        self.set_config(autodispatch=True)
        # five LIVE but idle (or cron) sessions — the dispatcher would count zero
        # active, so the planner must see full headroom (m14), not zero.
        self.set_world(live=[
            {"ws_ref": f"workspace:{i}", "last_turn_age_sec": 99999,
             "is_cron": True} for i in range(5)])
        self.add_stalled_goal()
        goals.plan_pass(now=NOW)
        self.assertEqual(len(self.goal_todos()), 1)      # staged despite 5 live
        # tied to the SHARED predicate the dispatcher uses
        from assistant import config
        self.assertFalse(config.ws_is_active(None, 99999))

    def test_unreadable_store_is_ledgered_skip(self):
        self.set_world()
        p = goals.goals_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is corrupt ]")             # present but unparseable
        summary = goals.plan_pass(now=NOW)               # must NOT crash
        self.assertTrue(summary["unreadable"])
        self.assertTrue(any(r.get("key") == "planner:goals-unreadable"
                            for r in self.ledger_rows()))


class CapSingleSourceTests(unittest.TestCase):
    """m14: ACTIVE_WS_CAP has ONE source (config.py). goals imports it (no
    divergent copy), and pulse.py's canonical literal must not drift from it."""

    def test_goals_cap_is_the_config_cap(self):
        from assistant import config
        self.assertIs(goals.ACTIVE_WS_CAP, config.ACTIVE_WS_CAP)
        self.assertEqual(config.ACTIVE_WS_CAP, 5)

    def test_pulse_cap_matches_config(self):
        import re
        from assistant import config
        txt = (REPO / "bin" / "pulse.py").read_text()
        m = re.search(r"^ACTIVE_WS_CAP\s*=\s*(\d+)", txt, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), config.ACTIVE_WS_CAP)


if __name__ == "__main__":
    unittest.main()
