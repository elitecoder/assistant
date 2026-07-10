"""Tests for src/assistant/goals.py (Keel M4): the goals store (load/validate/
atomic write), the human edit path (add/update/rerank/pause), the confirmation-
gated goal_update proposal path for automation, the MECHANICAL progress linker
(pure typed set-intersection), stall detection + week-keyed dedup, the goal_boost
ranking term in brief.py, the two new metrics, and a STRUCTURAL no-LLM proof
(R3: M4 adds zero new LLM spend).

Everything runs under a tmp $HOME (goals/decisions/ledger paths compute per
call). Named test_goals (sorts AFTER test_daemon) and stdlib-only (no pytest) so
it loads identically under python3.9 and python3.12 and never perturbs the
discovery-order parity a prior milestone flagged as load-bearing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import goals, brief, decisions  # noqa: E402

# A fixed LOCAL morning instant so ISO-week / stall math is tz-independent.
NOW = datetime(2026, 7, 2, 10, 0).timestamp()
DAY = 86400


class GoalsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def write_goals(self, store):
        p = goals.goals_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(store))


# ─── store load + validation ─────────────────────────────────────────────────

class StoreLoadTests(GoalsBase):
    def test_missing_store_is_safe_empty(self):
        s = goals.load_goals()
        self.assertEqual(s["goals"], [])
        self.assertFalse(s["_paused"])
        self.assertEqual(s["_schema"], goals.SCHEMA)

    def test_corrupt_store_never_raises(self):
        p = goals.goals_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is not json ]")
        self.assertEqual(goals.load_goals()["goals"], [])  # safe no-op

    def test_invalid_goals_dropped_valid_kept(self):
        self.write_goals({"_schema": 1, "_paused": False, "goals": [
            {"id": "goal-1", "rank": 1, "title": "ok", "outcome": "measurable"},
            {"id": "bad-id", "rank": 2, "title": "x", "outcome": "y"},  # id
            {"id": "goal-3", "rank": 3, "title": "no outcome", "outcome": ""},  # outcome
            {"id": "goal-4", "rank": "nope", "title": "t", "outcome": "o"},  # rank
        ]})
        s = goals.load_goals()
        self.assertEqual([g["id"] for g in s["goals"]], ["goal-1"])

    def test_normalize_fills_defaults(self):
        self.write_goals({"goals": [
            {"id": "goal-1", "rank": 1, "title": "t", "outcome": "o"}]})
        g = goals.load_goals()["goals"][0]
        self.assertEqual(g["stallAfterHours"], goals.DEFAULT_STALL_AFTER_HOURS)
        self.assertEqual(g["playbook"]["unattended"], goals.DEFAULT_UNATTENDED)
        self.assertEqual(g["budget"]["maxActiveWs"], 2)
        self.assertEqual(g["status"], "active")


# ─── human edit path ─────────────────────────────────────────────────────────

class HumanEditTests(GoalsBase):
    def test_add_requires_outcome(self):
        g, err = goals.add_goal(title="t", outcome="", now=NOW)
        self.assertIsNone(g)
        self.assertIn("outcome", err)

    def test_add_assigns_id_and_rank_atomically(self):
        g1, _ = goals.add_goal(title="a", outcome="oa", now=NOW)
        g2, _ = goals.add_goal(title="b", outcome="ob", now=NOW)
        self.assertEqual(g1["id"], "goal-1")
        self.assertEqual(g2["id"], "goal-2")
        self.assertEqual([g1["rank"], g2["rank"]], [1, 2])
        # atomic write left no tmp behind
        self.assertFalse(goals.goals_path().with_suffix(".json.tmp").exists())

    def test_update_rejects_non_editable_and_mechanical_fields(self):
        goals.add_goal(title="a", outcome="oa", now=NOW)
        _, err = goals.update_goal("goal-1", {"lastProgressAt": "2026-01-01T00:00:00Z"})
        self.assertIsNotNone(err)
        _, err2 = goals.update_goal("goal-1", {"status": "done"})
        self.assertIsNone(err2)
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "done")

    def test_rerank_reassigns_unique_ranks(self):
        for t in ("a", "b", "c"):
            goals.add_goal(title=t, outcome="o", now=NOW)
        ok, err = goals.rerank(["goal-3", "goal-1", "goal-2"], now=NOW)
        self.assertTrue(ok, err)
        ranks = {g["id"]: g["rank"] for g in goals.load_goals()["goals"]}
        self.assertEqual(ranks, {"goal-3": 1, "goal-1": 2, "goal-2": 3})

    def test_pause_flag(self):
        goals.add_goal(title="a", outcome="o", now=NOW)
        goals.set_paused(True, now=NOW)
        self.assertTrue(goals.load_goals()["_paused"])


# ─── confirmation-gated automation path ──────────────────────────────────────

class GoalUpdateProposalTests(GoalsBase):
    def test_automation_files_proposal_never_applies(self):
        goals.add_goal(title="a", outcome="o", now=NOW)
        entry = goals.file_goal_update_proposal(
            "goal-1", {"status": "done"}, reason="looks done", source="planner")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "goal_update")
        self.assertEqual(entry["status"], "pending")
        # store is UNCHANGED — the proposal never auto-applies
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "active")

    def test_proposal_deduped(self):
        goals.add_goal(title="a", outcome="o", now=NOW)
        first = goals.file_goal_update_proposal("goal-1", {"status": "done"},
                                                reason="x")
        dup = goals.file_goal_update_proposal("goal-1", {"status": "done"},
                                              reason="x")
        self.assertIsNotNone(first)
        self.assertIsNone(dup)


# ─── mechanical progress linker (pure) ───────────────────────────────────────

class ProgressLinkerTests(GoalsBase):
    def test_repo_token_normalization(self):
        goal = {"id": "goal-1", "links": {"repos": ["elitecoder/assistant"]}}
        arts = goals.ledger_artifacts([
            {"epoch": NOW - 3600, "repo": "assistant", "kind": "dispatch"}])
        self.assertEqual(goals.last_progress_at(goal, arts, now=NOW), NOW - 3600)

    def test_typed_matching_no_cross_type_collision(self):
        # a PR number 42 must NOT match a repo whose name is "42"
        goal = {"id": "goal-1", "links": {"repos": ["42"]}}
        arts = goals.pr_artifacts([
            {"state": "merged", "merged_epoch": NOW - 5, "number": 42}])
        self.assertIsNone(goals.last_progress_at(goal, arts, now=NOW))

    def test_only_merged_prs_count(self):
        goal = {"id": "g", "links": {"prs": [7]}}
        arts = goals.pr_artifacts([
            {"state": "open", "merged_epoch": NOW - 5, "number": 7}])
        self.assertIsNone(goals.last_progress_at(goal, arts, now=NOW))

    def test_resolved_decision_via_goal_refs(self):
        folded = {"dec-x": {"status": "accepted", "goal_refs": ["goal-1"],
                            "resolution": {"ts": goals.utc_iso(NOW - 100)},
                            "epoch": int(NOW - 100)}}
        arts = goals.decision_artifacts(folded)
        goal = {"id": "goal-1", "links": {}}
        self.assertEqual(goals.last_progress_at(goal, arts, now=NOW), NOW - 100)

    def test_done_todo_via_source(self):
        todo = {"items": [{"id": "td-9", "status": "done",
                           "source": "goal:goal-1:abc",
                           "statusUpdatedAt": goals.utc_iso(NOW - 200)}]}
        arts = goals.todo_artifacts(todo)
        goal = {"id": "goal-1", "links": {}}
        self.assertEqual(goals.last_progress_at(goal, arts, now=NOW), NOW - 200)

    def test_max_over_multiple_artifacts(self):
        goal = {"id": "goal-1", "links": {"repos": ["r"], "prs": [1]}}
        arts = (goals.ledger_artifacts([{"epoch": NOW - 5000, "repo": "r"}])
                + goals.pr_artifacts([{"state": "merged",
                                       "merged_epoch": NOW - 100, "number": 1}]))
        self.assertEqual(goals.last_progress_at(goal, arts, now=NOW), NOW - 100)

    def test_future_artifacts_ignored(self):
        goal = {"id": "goal-1", "links": {"repos": ["r"]}}
        arts = goals.ledger_artifacts([{"epoch": NOW + 5000, "repo": "r"}])
        self.assertIsNone(goals.last_progress_at(goal, arts, now=NOW))

    def test_stamp_progress_writes_store(self):
        goals.add_goal(title="a", outcome="o",
                       links={"repos": ["assistant"]}, now=NOW - 10 * DAY)
        # a ledger row that matches
        lp = goals.ledger_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"epoch": int(NOW - 3600), "repo": "assistant",
                                  "kind": "dispatch"}) + "\n")
        out = goals.stamp_progress(now=NOW)
        self.assertEqual(out["n"], 1)
        self.assertEqual(
            goals.parse_iso(goals.load_goals()["goals"][0]["lastProgressAt"]),
            NOW - 3600)

    def test_stamp_never_rewinds(self):
        goals.add_goal(title="a", outcome="o", links={"repos": ["r"]},
                       now=NOW - 10 * DAY)
        goals.update_goal("goal-1", {})  # no-op edit
        # manually set a newer lastProgressAt
        with goals._goals_lock():
            raw = goals._load_raw_unlocked()
            raw["goals"][0]["lastProgressAt"] = goals.utc_iso(NOW - 60)
            goals._save_goals_unlocked(raw)
        lp = goals.ledger_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"epoch": int(NOW - 3600), "repo": "r"}) + "\n")
        goals.stamp_progress(now=NOW)  # older than existing → no rewind
        self.assertEqual(
            goals.parse_iso(goals.load_goals()["goals"][0]["lastProgressAt"]),
            NOW - 60)


# ─── stall detection + week dedup ────────────────────────────────────────────

class StallTests(GoalsBase):
    def _goal(self, **over):
        g = {"id": "goal-1", "status": "active", "stallAfterHours": 48,
             "lastProgressAt": goals.utc_iso(NOW - 3 * DAY),
             "createdAt": goals.utc_iso(NOW - 5 * DAY), "links": {}}
        g.update(over)
        return g

    def test_stalled_when_past_threshold(self):
        self.assertTrue(goals.is_stalled(self._goal(), now=NOW))

    def test_not_stalled_when_fresh(self):
        g = self._goal(lastProgressAt=goals.utc_iso(NOW - 3600))
        self.assertFalse(goals.is_stalled(g, now=NOW))

    def test_not_stalled_when_inactive(self):
        self.assertFalse(goals.is_stalled(self._goal(status="done"), now=NOW))

    def test_new_goal_no_progress_stalls_from_createdAt(self):
        g = self._goal(lastProgressAt=None,
                       createdAt=goals.utc_iso(NOW - 3 * DAY))
        self.assertTrue(goals.is_stalled(g, now=NOW))

    def test_blocked_by_open_decision_not_stalled(self):
        # open a decision that references the goal
        ev = {"id": "e1", "source": "goal:goal-1", "external_id": "x",
              "kind": "goal_step", "title": "t", "ts": goals.utc_iso(NOW)}
        decisions.open_decision(event=ev, lane="staged", policy_id="planner",
                                action={"class": "research"},
                                goal_refs=["goal-1"], now=NOW)
        self.assertFalse(goals.is_stalled(self._goal(), now=NOW))

    def test_iso_week_key_stable_within_week(self):
        k1 = goals.iso_week_key("goal-1", NOW)
        k2 = goals.iso_week_key("goal-1", NOW + 2 * DAY)  # same ISO week
        self.assertEqual(k1, k2)
        self.assertTrue(re.match(r"^goal-stall:goal-1:\d{4}-W\d{2}$", k1))


# ─── goal_boost ranking term (brief.py) ──────────────────────────────────────

class GoalBoostTests(GoalsBase):
    def test_rank1_beats_rank3(self):
        self.assertGreater(brief.goal_boost_for_rank(1),
                           brief.goal_boost_for_rank(3))

    def test_cap_below_lane_band(self):
        cfg = brief.SCORE_CONFIG
        cap = cfg["goal_boost_by_rank"]["cap"]
        band = cfg["lane_base"]["staged"] - cfg["lane_base"]["digest"]
        self.assertLess(cap, band)  # a boosted goal can't out-band a lane
        self.assertLessEqual(brief.goal_boost_for_rank(1), cap)

    def test_no_goal_refs_zero_boost(self):
        self.assertEqual(brief._record_goal_boost({"goal_refs": []},
                                                  {"goal-1": 1}), 0.0)

    def test_boost_used_but_partition_keeps_escalate_on_top(self):
        # a goal-linked staged decision (max boost) must still rank BELOW an
        # escalate decision — the lane partition, not the score, guarantees it.
        recs = [
            {"schema": decisions.SCHEMA, "id": "dec-esc", "status": "open",
             "lane": "escalate", "urgency": "low", "epoch": int(NOW),
             "created_epoch": int(NOW), "goal_refs": []},
            {"schema": decisions.SCHEMA, "id": "dec-stg", "status": "open",
             "lane": "staged", "urgency": "now", "epoch": int(NOW),
             "created_epoch": int(NOW), "goal_refs": ["goal-1"]},
        ]
        q = brief._build_queue(recs, NOW, goal_ranks={"goal-1": 1})
        self.assertEqual(q[0]["lane"], "escalate")
        # and the boost WAS applied to the staged row
        stg = next(r for r in q if r["id"] == "dec-stg")
        self.assertGreater(stg["goal_boost"], 0)


# ─── metrics ─────────────────────────────────────────────────────────────────

class MetricsTests(GoalsBase):
    def test_goals_progressed_overnight(self):
        goals.add_goal(title="a", outcome="o", now=NOW)
        with goals._goals_lock():
            raw = goals._load_raw_unlocked()
            raw["goals"][0]["lastProgressAt"] = goals.utc_iso(NOW - 3600)
            goals._save_goals_unlocked(raw)
        self.assertEqual(goals.goals_progressed_overnight(now=NOW), 1)
        # a progress older than 24h does not count
        with goals._goals_lock():
            raw = goals._load_raw_unlocked()
            raw["goals"][0]["lastProgressAt"] = goals.utc_iso(NOW - 5 * DAY)
            goals._save_goals_unlocked(raw)
        self.assertEqual(goals.goals_progressed_overnight(now=NOW), 0)

    def test_staged_accept_rate(self):
        # two goal-linked decisions resolved: 1 accepted, 1 rejected → 0.5
        for i, status in enumerate(("accepted", "rejected")):
            ev = {"id": f"e{i}", "source": "goal:goal-1", "external_id": f"x{i}",
                  "kind": "goal_step", "title": "t", "ts": goals.utc_iso(NOW)}
            rec, _ = decisions.open_decision(
                event=ev, lane="staged", policy_id="planner",
                action={"class": "research"}, goal_refs=["goal-1"], now=NOW - 100)
            decisions.transition(rec["id"], status, via="test", now=NOW - 50)
        self.assertEqual(goals.staged_accept_rate(now=NOW), 0.5)

    def test_metrics_appear_in_daily_row(self):
        goals.add_goal(title="a", outcome="o", now=NOW)
        doc = brief.build_brief(now=NOW)
        row = brief.compute_daily_metrics(doc, now=NOW)
        self.assertIn("goals_progressed_overnight", row)
        self.assertIn("staged_accept_rate", row)


# ─── structural no-LLM proof (R3) ────────────────────────────────────────────

class NoLLMStructuralTests(unittest.TestCase):
    """M4 must add ZERO new LLM spend. The mechanical invariant, proven
    STRUCTURALLY via the AST (not a fragile text grep that a `.claude` path or a
    docstring would trip): the M4 modules import NO process-spawning or LLM-SDK
    module — so there is physically no path from goals.py / plan-next-actions.py
    to a `claude` CLI, an Anthropic/Bedrock SDK, or the metered runner — and make
    NO os.system/os.popen/exec/eval call."""

    FILES = [REPO / "src" / "assistant" / "goals.py",
             REPO / "bin" / "plan-next-actions.py"]
    FORBIDDEN_IMPORTS = {"subprocess", "anthropic", "boto3", "botocore",
                         "metering", "metered_llm"}
    FORBIDDEN_CALLS = {"system", "popen", "exec", "eval", "Popen", "spawn",
                       "spawnv", "execv", "execvp"}

    def _imports_and_calls(self, tree):
        import ast
        imports, calls = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imports.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    calls.add(fn.attr)
                elif isinstance(fn, ast.Name):
                    calls.add(fn.id)
        return imports, calls

    def test_no_llm_or_subprocess_call_paths(self):
        import ast
        for f in self.FILES:
            tree = ast.parse(f.read_text())
            imports, calls = self._imports_and_calls(tree)
            bad_imp = imports & self.FORBIDDEN_IMPORTS
            self.assertEqual(bad_imp, set(),
                             f"{f.name} imports forbidden module(s): {bad_imp}")
            bad_call = calls & self.FORBIDDEN_CALLS
            self.assertEqual(bad_call, set(),
                             f"{f.name} makes forbidden call(s): {bad_call}")


if __name__ == "__main__":
    unittest.main()
