"""Tests for the Keel M6 Strategist (src/assistant/strategist.py + the goals
planner wiring + the brief surfacing + the curator target).

Governance/safety is the whole point of M6 (first new LLM caller since M2), so
the suite proves, with NO live LLM and NO network (every draft is INJECTED):

  • WHAT-not-WHETHER + no-action-path: Strategist output is TEXT ONLY and can
    never reach an action class (the structural M6 twin of M2's no-auto-lane);
  • strict-JSON schema validation: an out-of-playbook step_class is rejected;
  • malformed/raising LLM output → template fallback, NEVER a TODO, never a
    crash, never a blocked pulse;
  • throttle ≤1/goal/day persisted across pulses AND restarts (ledger-based);
  • over-ceiling → ledgered skip (Strategist shed FIRST; Observer/triage never);
  • both mechanical auto-pause twins (accept-rate <50%, expired-unseen growth);
  • the >=10 goal+world → step_class-within-playbook rubric fixtures;
  • Plane-2 curator prose can never gate an action.
"""
from __future__ import annotations

import ast
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))

from assistant import brief, decisions, goals, strategist  # noqa: E402

FIXTURES = json.loads((REPO / "evals/strategist/fixtures.json").read_text())


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        (self._tmp / ".claude" / "cache").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self._tmp)
        self.now = time.time()

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    # ─ helpers ─
    def _write_world(self, built_epoch=None, live=None):
        p = self._tmp / ".claude" / "cache" / "world.json"
        built = strategist.utc_iso(built_epoch if built_epoch is not None else self.now)
        p.write_text(json.dumps({"_meta": {"built_at": built},
                                 "live_sessions": live or []}))

    def _write_goal_store(self, goal):
        p = self._tmp / ".claude" / "assistant-goals.json"
        p.write_text(json.dumps({"_schema": 1, "_paused": False, "goals": [goal]}))

    def _cfg(self, strategist_cfg):
        p = self._tmp / ".assistant" / "comms" / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"strategist": strategist_cfg}))

    def _cost_row(self, est_usd, epoch=None):
        p = strategist.cost_ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"ts": strategist.utc_iso(epoch or self.now),
                                "caller": "strategist", "est_usd": est_usd}) + "\n")

    def _write_decisions(self, recs):
        p = decisions.decisions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def _ledger_keys(self):
        p = strategist.ledger_path()
        if not p.exists():
            return []
        return [json.loads(l).get("key") for l in p.read_text().splitlines() if l.strip()]


# ─── the >=10 goal+world → step_class-within-playbook rubric fixtures ─────────

class FixtureRubricTests(_Base):
    def test_at_least_ten_fixtures(self):
        self.assertGreaterEqual(len(FIXTURES["cases"]), 10)

    def test_each_fixture_planner_picks_playbook_class_and_draft_scores(self):
        for case in FIXTURES["cases"]:
            with self.subTest(case=case["name"]):
                goal = goals._normalize_goal(case["goal"])
                playbook = strategist.playbook_classes(goal)
                # 1. The DETERMINISTIC planner picks the expected class, and it
                #    is INSIDE the goal's playbook enum (never invented).
                step = goals.select_next_step(goal, {}, {})
                self.assertIsNotNone(step)
                step_class = step[0]
                self.assertEqual(step_class, case["expected_step_class"])
                self.assertIn(step_class, playbook)
                # 2. The model draft scores on the deterministic rubric: it
                #    validates (echoed class in playbook, non-empty title+detail)
                #    and meets the min-detail length.
                drafted = strategist.validate_draft(case["draft"], goal, step_class)
                self.assertIsNotNone(drafted, f"{case['name']} draft rejected")
                title, detail = drafted
                self.assertTrue(title.strip())
                self.assertGreaterEqual(len(detail), case["rubric"]["min_detail_chars"])
                # 3. A `bad_draft`, when present, is REJECTED → template fallback.
                if "bad_draft" in case:
                    self.assertIsNone(
                        strategist.validate_draft(case["bad_draft"], goal, step_class),
                        f"{case['name']} bad_draft should be rejected")


# ─── strict-JSON schema validation ───────────────────────────────────────────

class ValidateDraftTests(_Base):
    GOAL = {"id": "goal-1", "title": "T", "outcome": "O",
            "playbook": {"unattended": ["research"], "gated": ["code-change"]}}

    def test_out_of_playbook_class_rejected(self):
        # The LLM cannot invent an unattended action class.
        self.assertIsNone(strategist.validate_draft(
            {"step_class": "email.send", "title": "x", "detail": "yyyy"},
            self.GOAL, "research"))

    def test_class_within_playbook_accepted(self):
        self.assertIsNotNone(strategist.validate_draft(
            {"step_class": "code-change", "title": "x", "detail": "yyyy"},
            self.GOAL, "research"))

    def test_missing_or_empty_fields_rejected(self):
        for raw in ({"title": "x"}, {"detail": "y"}, {"title": "", "detail": "y"},
                    {"title": "x", "detail": "   "}, "not a dict", None, 42,
                    {"title": 1, "detail": "y"}):
            self.assertIsNone(strategist.validate_draft(raw, self.GOAL, "research"))

    def test_returns_text_only_two_tuple_of_str(self):
        out = strategist.validate_draft(
            {"title": "x", "detail": "yyyy"}, self.GOAL, "research")
        self.assertIsInstance(out, tuple)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(isinstance(x, str) for x in out))


# ─── WHAT-not-WHETHER + malformed fallback ───────────────────────────────────

class UpgradeStepTextTests(_Base):
    GOAL = {"id": "goal-1", "title": "Ship X", "outcome": "X done",
            "playbook": {"unattended": ["research"], "gated": []},
            "budget": {"maxStrategistCallsPerDay": 1}}

    def test_valid_draft_upgrades_text(self):
        out = strategist.upgrade_step_text(
            self.GOAL, "research", "TT", "TD",
            llm_draft=lambda g, sc, tt, td: {"step_class": "research",
                                             "title": "DRAFT", "detail": "better"},
            now=self.now)
        self.assertEqual(out, ("DRAFT", "better"))

    def test_malformed_json_falls_back_to_template_not_crash(self):
        for bad in ("not a dict", None, {"nope": 1}, [1, 2, 3]):
            out = strategist.upgrade_step_text(
                self.GOAL, "research", "TT", "TD",
                llm_draft=lambda g, sc, tt, td: bad, now=self.now)
            self.assertEqual(out, ("TT", "TD"))
            # a fresh goal each time so the throttle doesn't mask the fallback
            self.GOAL = dict(self.GOAL, id=f"goal-{id(bad) % 999}")

    def test_raising_llm_falls_back_to_template(self):
        def boom(g, sc, tt, td):
            raise RuntimeError("llm exploded")
        out = strategist.upgrade_step_text(
            self.GOAL, "research", "TT", "TD", llm_draft=boom, now=self.now)
        self.assertEqual(out, ("TT", "TD"))

    def test_always_returns_two_tuple_of_str(self):
        for ld in (lambda *a: {"title": "x", "detail": "yy"},
                   lambda *a: None,
                   lambda *a: (_ for _ in ()).throw(ValueError())):
            out = strategist.upgrade_step_text(
                dict(self.GOAL, id="goal-9"), "research", "TT", "TD",
                llm_draft=ld, now=self.now)
            self.assertIsInstance(out, tuple)
            self.assertEqual(len(out), 2)
            self.assertTrue(all(isinstance(x, str) for x in out))

    def test_malformed_output_never_becomes_a_todo(self):
        # Full planner path with autoDispatch ON: a malformed draft must stage
        # the TEMPLATE text, never a TODO built from bad LLM output — and never
        # crash the pass.
        self._write_world()
        self._cfg({})
        (self._tmp / ".assistant" / "comms" / "config.json").write_text(
            json.dumps({"planner": {"autoDispatch": True}, "strategist": {}}))
        goal = {"id": "goal-1", "rank": 1, "title": "Ship X", "outcome": "X done",
                "status": "active", "stallAfterHours": 1,
                "createdAt": strategist.utc_iso(self.now - 100000),
                "playbook": {"unattended": ["research"], "gated": []},
                "budget": {"maxStrategistCallsPerDay": 1, "maxStagedTodosPerNight": 2,
                           "maxActiveWs": 2}}
        self._write_goal_store(goal)

        def draft(goal, sc, tt, td, now):
            return strategist.upgrade_step_text(
                goal, sc, tt, td,
                llm_draft=lambda *a: "garbage not json", now=now)
        summ = goals.plan_pass(now=self.now, strategist_draft=draft)
        self.assertEqual(len(summ["staged_todos"]), 1)
        todo = json.loads((self._tmp / ".claude" / "assistant-todo.json").read_text())
        item = todo["items"][0]
        self.assertNotIn("garbage", (item["title"] + item["detail"]).lower())
        # template text is used
        self.assertIn("research", item["detail"].lower())


# ─── throttle ≤1/goal/day, persisted across pulses AND restarts ──────────────

class ThrottleTests(_Base):
    GOAL = {"id": "goal-1", "title": "T", "outcome": "O",
            "playbook": {"unattended": ["research"], "gated": []},
            "budget": {"maxStrategistCallsPerDay": 1}}

    def _draft(self, goal, now):
        return strategist.upgrade_step_text(
            goal, "research", "TT", "TD",
            llm_draft=lambda *a: {"title": "DRAFT", "detail": "better one"},
            now=now)

    def test_second_call_same_day_uses_template(self):
        self.assertEqual(self._draft(self.GOAL, self.now), ("DRAFT", "better one"))
        # second call same day → throttled → template (a "different pulse")
        self.assertEqual(self._draft(self.GOAL, self.now + 60), ("TT", "TD"))
        self.assertIn(f"strategist:skip:throttled:goal-1", self._ledger_keys())

    def test_throttle_persists_across_restart(self):
        self._draft(self.GOAL, self.now)
        # Simulate a process restart: re-import the module fresh; the ledger on
        # disk is the durable throttle state, so the reloaded module still skips.
        import importlib
        import assistant.strategist as s2
        importlib.reload(s2)
        self.assertEqual(
            s2.upgrade_step_text(self.GOAL, "research", "TT", "TD",
                                 llm_draft=lambda *a: {"title": "D", "detail": "dd"},
                                 now=self.now + 120),
            ("TT", "TD"))
        importlib.reload(strategist)

    def test_next_day_allows_again(self):
        self._draft(self.GOAL, self.now)
        out = self._draft(self.GOAL, self.now + 24 * 3600 + 60)
        self.assertEqual(out, ("DRAFT", "better one"))

    def test_budget_of_two_allows_two(self):
        goal = dict(self.GOAL, budget={"maxStrategistCallsPerDay": 2})
        self.assertEqual(self._draft(goal, self.now), ("DRAFT", "better one"))
        self.assertEqual(self._draft(goal, self.now + 60), ("DRAFT", "better one"))
        self.assertEqual(self._draft(goal, self.now + 120), ("TT", "TD"))


# ─── daily cost ceiling: sheds Strategist FIRST, ledgered ────────────────────

class CeilingTests(_Base):
    GOAL = {"id": "goal-1", "title": "T", "outcome": "O",
            "playbook": {"unattended": ["research"], "gated": []},
            "budget": {"maxStrategistCallsPerDay": 1}}

    def test_over_ceiling_sheds_strategist_ledgered(self):
        self._cfg({"dailyCostCeilingUsd": 1.0})
        self._cost_row(5.0)  # today's spend blows the ceiling
        self.assertTrue(strategist.over_ceiling(self.now))
        out = strategist.upgrade_step_text(
            self.GOAL, "research", "TT", "TD",
            llm_draft=lambda *a: {"title": "DRAFT", "detail": "better"},
            now=self.now)
        self.assertEqual(out, ("TT", "TD"))  # shed → template
        self.assertIn("strategist:skip:ceiling-shed:goal-1", self._ledger_keys())

    def test_under_ceiling_drafts(self):
        self._cfg({"dailyCostCeilingUsd": 100.0})
        self._cost_row(1.0)
        out = strategist.upgrade_step_text(
            self.GOAL, "research", "TT", "TD",
            llm_draft=lambda *a: {"title": "DRAFT", "detail": "better"},
            now=self.now)
        self.assertEqual(out, ("DRAFT", "better"))

    def test_day_spend_ignores_yesterday(self):
        self._cost_row(99.0, epoch=self.now - 2 * 24 * 3600)  # 2 days ago
        self.assertEqual(strategist.day_spend_usd(self.now), 0.0)

    def test_ceiling_never_sheds_observer_or_triage_structural(self):
        # Only the Strategist module consults the ceiling. Observer/triage code
        # never reference over_ceiling/day_spend_usd, so the ceiling can not
        # shed them (design: 'sheds Strategist/Drafter, never Observer').
        for name in ("triage.py",):
            src = (SRC / "assistant" / name).read_text()
            self.assertNotIn("over_ceiling", src)
            self.assertNotIn("day_spend_usd", src)
        pulse_src = (REPO / "bin" / "pulse.py").read_text()
        # the Observer batch caller must not gate on the ceiling either
        self.assertNotIn("over_ceiling", pulse_src)


# ─── mechanical auto-pause twins ─────────────────────────────────────────────

class AutoPauseTests(_Base):
    def _resolved(self, i, status, goal_refs, via="human", lane="staged"):
        return {"schema": decisions.SCHEMA, "id": f"dec-{i}", "epoch": int(self.now),
                "status": status, "lane": lane, "goal_refs": goal_refs,
                "resolution": {"ts": strategist.utc_iso(self.now - 3600), "via": via}}

    def test_accept_rate_below_floor_pauses(self):
        recs = [self._resolved(1, "accepted", ["goal-1"])]
        recs += [self._resolved(i, "rejected", ["goal-1"]) for i in range(2, 6)]
        self._write_decisions(recs)  # 1 kept / 5 resolved = 0.2 < 0.5, n=5>=4
        self.assertEqual(strategist.auto_pause_reason(self.now), "accept-rate")
        self.assertTrue(strategist.auto_paused(self.now))
        self.assertTrue(any(k.startswith("strategist:autopause:accept-rate")
                            for k in self._ledger_keys()))

    def test_small_sample_does_not_pause(self):
        # day-one guard: 1 rejected of 1 resolved is <50% but sample < min → no pause
        self._write_decisions([self._resolved(1, "rejected", ["goal-1"])])
        self.assertIsNone(strategist.auto_pause_reason(self.now))
        self.assertFalse(strategist.auto_paused(self.now))

    def test_healthy_accept_rate_does_not_pause(self):
        recs = [self._resolved(i, "accepted", ["goal-1"]) for i in range(1, 6)]
        self._write_decisions(recs)  # 5/5 = 1.0
        self.assertIsNone(strategist.auto_pause_reason(self.now))

    def test_expired_unseen_growth_pauses(self):
        # 5 expired-unseen decisions (no goal_refs, so accept-rate stays 0/0 and
        # this ISOLATES the second trigger) over the limit → pause.
        recs = [self._resolved(i, "expired", [], via="ttl", lane="escalate")
                for i in range(1, 6)]
        self._write_decisions(recs)
        self.assertEqual(strategist.expired_unseen(self.now), 5)
        self.assertEqual(strategist.auto_pause_reason(self.now), "expired-unseen")
        self.assertTrue(strategist.auto_paused(self.now))
        self.assertTrue(any(k.startswith("strategist:autopause:expired-unseen")
                            for k in self._ledger_keys()))

    def test_expired_under_limit_does_not_pause(self):
        recs = [self._resolved(i, "expired", [], via="ttl", lane="escalate")
                for i in range(1, 3)]
        self._write_decisions(recs)
        self.assertIsNone(strategist.auto_pause_reason(self.now))

    def test_auto_pause_makes_upgrade_return_template(self):
        recs = [self._resolved(1, "accepted", ["goal-1"])]
        recs += [self._resolved(i, "rejected", ["goal-1"]) for i in range(2, 6)]
        self._write_decisions(recs)
        goal = {"id": "goal-1", "title": "T", "outcome": "O",
                "playbook": {"unattended": ["research"], "gated": []},
                "budget": {"maxStrategistCallsPerDay": 1}}
        out = strategist.upgrade_step_text(
            goal, "research", "TT", "TD",
            llm_draft=lambda *a: {"title": "DRAFT", "detail": "x"}, now=self.now)
        self.assertEqual(out, ("TT", "TD"))
        self.assertIn("strategist:skip:auto-paused:goal-1", self._ledger_keys())


# ─── WHAT-not-WHETHER: staged action class stays Python-owned ────────────────

class WhatNotWhetherPlannerTests(_Base):
    def test_llm_echoing_different_class_never_changes_staged_class(self):
        # Plan a stalled goal whose FIRST playbook class is doc-draft. Even if
        # the LLM echoes a DIFFERENT (but in-playbook) class, the STAGED action
        # class must remain the Python-chosen doc-draft.
        self._write_world()
        (self._tmp / ".assistant" / "comms" / "config.json").parent.mkdir(
            parents=True, exist_ok=True)
        (self._tmp / ".assistant" / "comms" / "config.json").write_text(
            json.dumps({"strategist": {}}))  # autoDispatch OFF → stages a decision
        goal = {"id": "goal-1", "rank": 1, "title": "Doc goal", "outcome": "done",
                "status": "active", "stallAfterHours": 1,
                "createdAt": strategist.utc_iso(self.now - 100000),
                "playbook": {"unattended": ["doc-draft", "research"], "gated": []},
                "budget": {"maxStrategistCallsPerDay": 1, "maxStagedTodosPerNight": 2,
                           "maxActiveWs": 2}}
        self._write_goal_store(goal)

        def draft(goal, sc, tt, td, now):
            # LLM echoes 'research' (in playbook) with drafted text, but the
            # planner chose 'doc-draft'.
            return strategist.upgrade_step_text(
                goal, sc, tt, td,
                llm_draft=lambda *a: {"step_class": "research",
                                      "title": "DRAFTED TITLE", "detail": "drafted detail"},
                now=now)
        summ = goals.plan_pass(now=self.now, strategist_draft=draft)
        self.assertEqual(len(summ["staged_decisions"]), 1)
        folded = decisions.fold(decisions.read_log())
        rec = next(iter(folded.values()))
        # drafted TEXT landed …
        self.assertEqual(rec["title"], "DRAFTED TITLE")
        # … but the staged action class is the Python-chosen doc-draft, never
        # the LLM-echoed 'research'.
        self.assertEqual((rec.get("recommended") or {}).get("class"), "doc-draft")


# ─── structural no-action-path invariant (M6 twin of M2 no-auto-lane) ────────

class StructuralNoActionPathTests(unittest.TestCase):
    """There is NO code path from Strategist LLM output to an action class,
    lane, or dispatch. Proven structurally: (1) the pure module never calls the
    decision WRITER, never touches autoDispatch/dispatch/cmux, and never imports
    a subprocess/LLM-SDK (so it cannot itself act); (2) validate_draft +
    upgrade_step_text return TEXT ONLY. The LLM's echoed class is re-validated
    against the playbook and then DISCARDED — the planner stages the
    Python-chosen class."""

    MOD = SRC / "assistant" / "strategist.py"
    FORBIDDEN_IMPORTS = {"subprocess", "anthropic", "boto3", "botocore",
                         "metering", "metered_llm", "importlib"}

    def _src(self):
        return self.MOD.read_text()

    def test_no_llm_or_subprocess_import(self):
        tree = ast.parse(self._src())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertEqual(imports & self.FORBIDDEN_IMPORTS, set())

    def test_never_calls_decision_writer_or_dispatch(self):
        # AST identifier check (not a text grep — the docstring legitimately
        # NAMES autoDispatch/_stage_* while explaining what it must NOT do). No
        # Name/Attribute in the module's CODE may reference the decision writer,
        # the TODO/decision stagers, autoDispatch, or the dispatcher — so there
        # is physically no path from Strategist output to an action.
        tree = ast.parse(self._src())
        idents = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                idents.add(node.attr)
            elif isinstance(node, ast.Name):
                idents.add(node.id)
        forbidden = {"open_decision", "autoDispatch", "_stage_todo",
                     "_stage_decision", "dispatch_todo", "transition"}
        self.assertEqual(idents & forbidden, set(),
                         f"strategist code references action verbs: "
                         f"{idents & forbidden}")
        # open_decisions (the READER, plural) is allowed and expected.
        self.assertIn("open_decisions", idents)

    def test_upgrade_and_validate_return_text_only(self):
        # No return annotation or code path yields an action class: both public
        # entrypoints return tuple[str, str] | None.
        goal = {"id": "g", "title": "t", "outcome": "o",
                "playbook": {"unattended": ["research"], "gated": []}}
        v = strategist.validate_draft({"title": "a", "detail": "bb"}, goal, "research")
        self.assertTrue(isinstance(v, tuple) and all(isinstance(x, str) for x in v))
        u = strategist.upgrade_step_text(
            goal, "research", "TT", "TD",
            llm_draft=lambda *a: {"title": "a", "detail": "bb"}, now=time.time())
        self.assertTrue(isinstance(u, tuple) and all(isinstance(x, str) for x in u))


# ─── Plane-2 curator target: prose can never gate an action ──────────────────

class CuratorStrategistTargetTests(unittest.TestCase):
    def test_curator_has_strategist_target_pointing_at_the_prompt(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "curator_m6", str(REPO / "bin" / "assistant-curator.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn("strategist", mod.TARGETS)
        self.assertEqual(mod.TARGETS["strategist"]["path"],
                         REPO / "prompts/strategist-draft-prompt.md")

    def test_prose_cannot_widen_the_action_set(self):
        # Plane 2 is prose for the LLM drafter only. Even if a lesson told the
        # model to emit a brand-new class, validate_draft re-checks the echoed
        # class against the goal's playbook IN CODE and rejects it — so no prose
        # can ever gate/ungate/widen an action.
        goal = {"id": "g", "title": "t", "outcome": "o",
                "playbook": {"unattended": ["research"], "gated": []}}
        self.assertIsNone(strategist.validate_draft(
            {"step_class": "email.send", "title": "x", "detail": "yyyy"},
            goal, "research"))

    def test_pure_module_never_reads_the_prompt(self):
        # The prose lives ONLY in the subprocess caller's prompt; the pure
        # gating module never reads it, so prose cannot influence any gate.
        self.assertNotIn("strategist-draft-prompt",
                         (SRC / "assistant" / "strategist.py").read_text())


# ─── nightly decision-context pre-research (idle-gated, draft-only) ──────────

class PreResearchTests(_Base):
    def _open_decision(self, dec_id="dec-1"):
        recs = [{"schema": decisions.SCHEMA, "id": dec_id, "epoch": int(self.now),
                 "status": decisions.OPEN, "lane": "staged", "goal_refs": [],
                 "title": "Do the thing", "snippet": "context please",
                 "source": "github", "kind": "review_requested", "refs": {}}]
        self._write_decisions(recs)

    def test_writes_context_on_idle_capacity(self):
        self._write_world()  # fresh, no live sessions → idle
        self._open_decision("dec-1")
        summ = strategist.pre_research_pass(
            self.now, llm_context=lambda dec: "## Context\n\nHere is the research.")
        self.assertEqual(summ["researched"], ["dec-1"])
        self.assertEqual(strategist.read_context("dec-1").strip(),
                         "## Context\n\nHere is the research.")

    def test_stale_world_skips_ledgered(self):
        self._write_world(built_epoch=self.now - 99999)  # stale
        self._open_decision()
        summ = strategist.pre_research_pass(self.now, llm_context=lambda d: "x")
        self.assertFalse(summ["idle"])
        self.assertIn("stale-world", [s.get("reason") for s in summ["skipped"]])

    def test_busy_fleet_never_steals_capacity(self):
        # No headroom: ACTIVE_WS_CAP live active sessions → no idle capacity.
        live = [{"ws_ref": f"ws{i}", "agent_status": "working",
                 "last_turn_age_sec": 1} for i in range(goals.ACTIVE_WS_CAP)]
        self._write_world(live=live)
        self._open_decision()
        summ = strategist.pre_research_pass(self.now, llm_context=lambda d: "x")
        self.assertIn("no-idle-headroom", [s.get("reason") for s in summ["skipped"]])
        self.assertEqual(summ["researched"], [])

    def test_over_ceiling_skips_pre_research_ledgered(self):
        self._cfg({"dailyCostCeilingUsd": 0.5})
        self._cost_row(5.0)
        self._write_world()
        self._open_decision()
        summ = strategist.pre_research_pass(self.now, llm_context=lambda d: "x")
        self.assertIn("ceiling-shed", [s.get("reason") for s in summ["skipped"]])

    def test_per_decision_throttle_across_pulses(self):
        self._write_world()
        self._open_decision("dec-1")
        strategist.pre_research_pass(self.now, llm_context=lambda d: "first")
        # a second pulse same day must not re-research dec-1 (context exists AND
        # the per-decision-per-day key is ledgered)
        summ = strategist.pre_research_pass(self.now + 60,
                                            llm_context=lambda d: "second")
        self.assertEqual(summ["researched"], [])
        self.assertEqual(strategist.read_context("dec-1").strip(), "first")

    def test_brief_surfaces_prepared_context(self):
        self._write_world()
        strategist.write_context("dec-42", "## Prepared\n\nthe draft context",
                                 self.now)
        ctx = brief._decision_context("dec-42")
        self.assertIn("Prepared", ctx)


if __name__ == "__main__":
    unittest.main()
