"""Tests for src/assistant/policy.py — the deterministic policy engine
(Keel M2): load/validate, ordered first-match-wins laning, the in-code hard
invariants (unmatched never drops, ambiguity escalates, auto only via a
policy id, the triage lane map structurally lacks `auto`), the bootstrap
install, the per-rule fixture coverage gate over evals/policy/, and the
policy-proposal miner.

unittest style so the suite runs under `python3 -m unittest discover tests`.
Everything runs against a tmp $HOME — policy.py computes every path per call.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import policy  # noqa: E402


def make_event(**over) -> dict:
    ev = {
        "schema": "world-event/1",
        "id": "eid-1",
        "ts": "2026-07-09T12:00:00Z",
        "epoch": 1783000000,
        "source": "cmux",
        "kind": "needs_input",
        "external_id": "cmux:workspace:7:needs_input:1:aa",
        "actor": None,
        "title": "workspace:7 needs_input",
        "snippet": "approve?",
        "url": None,
        "refs": {"ws_ref": "workspace:7"},
        "raw_path": None,
    }
    ev.update(over)
    return ev


def rule(rid="r1", source="cmux", kind="needs_input", lane="escalate", **over):
    r = {"id": rid, "match": {"source": source, "kind": kind},
         "lane": lane, "action": None, "urgency": None, "ttl_h": None,
         "pageable": False, "enabled": True}
    r.update(over)
    return r


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def write_policies(self, policies):
        p = policy.policies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 1, "policies": policies}))


# ─── structural invariants ───────────────────────────────────────────────────

class TriageLaneMapStructuralTests(unittest.TestCase):
    """THE M2 structural test: the triage LLM's complete lane vocabulary has
    no `auto` key (and no `drop` key) — an LLM output string can never mint
    an automatic action or a silent drop, by construction."""

    def test_lane_map_has_no_auto_key(self):
        self.assertNotIn("auto", policy.TRIAGE_LANE_MAP)

    def test_lane_map_has_no_drop_key(self):
        self.assertNotIn("drop", policy.TRIAGE_LANE_MAP)

    def test_lane_map_values_are_subset_of_lanes_minus_auto_and_drop(self):
        self.assertTrue(set(policy.TRIAGE_LANE_MAP.values())
                        <= set(policy.LANES) - {"auto", "drop"})

    def test_valid_triage_lane_refuses_auto_and_drop_and_garbage(self):
        self.assertIsNone(policy.valid_triage_lane("auto"))
        self.assertIsNone(policy.valid_triage_lane("drop"))
        self.assertIsNone(policy.valid_triage_lane("AUTO"))
        self.assertIsNone(policy.valid_triage_lane(None))
        self.assertIsNone(policy.valid_triage_lane(["escalate"]))
        self.assertEqual(policy.valid_triage_lane("staged"), "staged")
        self.assertEqual(policy.valid_triage_lane("escalate"), "escalate")
        self.assertEqual(policy.valid_triage_lane("digest"), "digest")


# ─── validation + load ───────────────────────────────────────────────────────

class ValidateRuleTests(unittest.TestCase):
    def test_valid_rule_passes(self):
        self.assertIsNone(policy.validate_rule(rule()))

    def test_missing_id_fails(self):
        r = rule()
        del r["id"]
        self.assertIn("missing id", policy.validate_rule(r))

    def test_bad_lane_fails(self):
        self.assertIn("invalid lane", policy.validate_rule(rule(lane="yolo")))

    def test_unknown_predicate_op_fails(self):
        r = rule()
        r["match"]["predicates"] = [{"field": "title", "op": "sounds_like",
                                     "value": "x"}]
        self.assertIn("unknown predicate op", policy.validate_rule(r))

    def test_bad_regex_fails(self):
        r = rule()
        r["match"]["predicates"] = [{"field": "title", "op": "regex",
                                     "value": "("}]
        self.assertIn("bad regex", policy.validate_rule(r))

    def test_auto_without_action_fails(self):
        self.assertIn("auto lane requires",
                      policy.validate_rule(rule(lane="auto")))

    def test_auto_with_unknown_action_class_fails(self):
        r = rule(lane="auto", action={"class": "email.send"})
        self.assertIn("auto lane requires", policy.validate_rule(r))

    def test_auto_with_standing_action_passes(self):
        r = rule(lane="auto", action={"class": "todo.create", "params": {}})
        self.assertIsNone(policy.validate_rule(r))


class LoadPoliciesTests(HomeTestCase):
    def test_missing_file_is_a_load_error(self):
        rules, invalid, error = policy.load_policies()
        self.assertEqual(rules, [])
        self.assertIsNotNone(error)

    def test_garbage_file_is_a_load_error(self):
        p = policy.policies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        _, _, error = policy.load_policies()
        self.assertIn("unreadable", error)

    def test_wrong_shape_is_a_load_error(self):
        p = policy.policies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"policies": "nope"}))
        _, _, error = policy.load_policies()
        self.assertIsNotNone(error)

    def test_invalid_rules_are_skipped_not_fatal(self):
        self.write_policies([rule("good"), rule("bad", lane="yolo"),
                             rule("good2", kind="work_complete")])
        rules, invalid, error = policy.load_policies()
        self.assertIsNone(error)
        self.assertEqual([r["id"] for r in rules], ["good", "good2"])
        self.assertEqual(len(invalid), 1)

    def test_duplicate_ids_keep_first_only(self):
        self.write_policies([rule("dup", lane="digest"), rule("dup")])
        rules, invalid, _ = policy.load_policies()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["lane"], "digest")
        self.assertTrue(any("duplicate" in e for e in invalid))


# ─── laning ──────────────────────────────────────────────────────────────────

class LaneEventTests(HomeTestCase):
    def test_first_match_wins_in_order(self):
        rules = [rule("first", lane="digest"), rule("second", lane="escalate")]
        laned = policy.lane_event(make_event(), rules)
        self.assertEqual(laned["policy_id"], "first")
        self.assertEqual(laned["lane"], "digest")

    def test_disabled_rules_never_match(self):
        rules = [rule("off", enabled=False), rule("on", lane="staged")]
        laned = policy.lane_event(make_event(), rules)
        self.assertEqual(laned["policy_id"], "on")

    def test_unmatched_event_is_unmatched_never_dropped(self):
        laned = policy.lane_event(make_event(source="github"), [rule()])
        self.assertEqual(laned["lane"], "unmatched")
        self.assertIsNone(laned["policy_id"])

    def test_load_error_lanes_everything_to_escalate(self):
        laned = policy.lane_event(make_event(), [], error="policies.json missing")
        self.assertEqual(laned["lane"], "escalate")
        self.assertIn("policy-load-failure", laned["reason"])

    def test_non_dict_event_escalates(self):
        laned = policy.lane_event("garbage", [rule()])
        self.assertEqual(laned["lane"], "escalate")

    def test_wildcard_source_and_kind(self):
        rules = [rule("any", source="*", kind="*", lane="digest")]
        self.assertEqual(policy.lane_event(
            make_event(source="x", kind="y"), rules)["policy_id"], "any")

    def test_default_ttl_per_lane(self):
        self.assertEqual(policy.lane_event(
            make_event(), [rule(lane="digest")])["ttl_h"], 24)
        self.assertEqual(policy.lane_event(
            make_event(), [rule(lane="staged")])["ttl_h"], 72)
        self.assertIsNone(policy.lane_event(
            make_event(), [rule(lane="escalate")])["ttl_h"])

    def test_explicit_ttl_beats_default(self):
        self.assertEqual(policy.lane_event(
            make_event(), [rule(lane="digest", ttl_h=6)])["ttl_h"], 6)

    def test_pageable_defaults_false_and_needs_explicit_rule(self):
        self.assertFalse(policy.lane_event(make_event(), [rule()])["pageable"])
        self.assertTrue(policy.lane_event(
            make_event(), [rule(pageable=True)])["pageable"])

    def test_predicates_all_must_pass(self):
        r = rule()
        r["match"]["predicates"] = [
            {"field": "snippet", "op": "contains", "value": "approve"},
            {"field": "refs.ws_ref", "op": "eq", "value": "workspace:7"},
        ]
        self.assertEqual(policy.lane_event(make_event(), [r])["policy_id"], "r1")
        r2 = rule("r2")
        r2["match"]["predicates"] = [
            {"field": "snippet", "op": "contains", "value": "approve"},
            {"field": "refs.ws_ref", "op": "eq", "value": "workspace:8"},
        ]
        self.assertEqual(policy.lane_event(make_event(), [r2])["lane"],
                         "unmatched")

    def test_predicate_op_matrix(self):
        ev = make_event(title="CI green on main", snippet="all checks passed")
        cases = [
            ({"field": "title", "op": "prefix", "value": "CI"}, True),
            ({"field": "title", "op": "regex", "value": r"green on \w+"}, True),
            ({"field": "kind", "op": "in", "value": ["needs_input", "x"]}, True),
            ({"field": "kind", "op": "ne", "value": "ping"}, True),
            ({"field": "refs.ws_ref", "op": "exists"}, True),
            ({"field": "refs.pr", "op": "missing"}, True),
            ({"field": "snippet", "op": "not_contains", "value": "failed"}, True),
            ({"field": "title", "op": "contains", "value": "red"}, False),
            ({"field": "refs.ws_ref", "op": "missing"}, False),
        ]
        for pred, want in cases:
            self.assertEqual(policy.eval_predicate(pred, ev), want,
                             msg=f"pred={pred}")

    def test_dotted_field_paths(self):
        self.assertEqual(policy.get_field(make_event(), "refs.ws_ref"),
                         "workspace:7")
        self.assertIsNone(policy.get_field(make_event(), "refs.nope.deeper"))


# ─── bootstrap install + per-rule fixture coverage ───────────────────────────

class BootstrapTests(HomeTestCase):
    def test_installs_bootstrap_when_absent(self):
        self.assertTrue(policy.ensure_policies_installed())
        rules, invalid, error = policy.load_policies()
        self.assertIsNone(error)
        self.assertEqual(invalid, [])
        self.assertGreaterEqual(len(rules), 8)

    def test_never_overwrites_existing_policies(self):
        p = policy.policies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 1, "policies": []}))
        self.assertFalse(policy.ensure_policies_installed())
        self.assertEqual(json.loads(p.read_text())["policies"], [])

    def test_second_install_is_a_noop(self):
        self.assertTrue(policy.ensure_policies_installed())
        self.assertFalse(policy.ensure_policies_installed())


class PolicyFixtureCoverageTests(HomeTestCase):
    """Every bootstrap rule id must have a fixture in evals/policy/<id>/ and
    the engine must lane that fixture's event to exactly that rule — the
    meta_gate-style coverage discipline extended to policies (design: every
    rule requires a passing fixture)."""

    FIXTURES = REPO / "evals" / "policy"

    def _bootstrap_rules(self):
        data = json.loads(policy.bootstrap_path().read_text())
        return [r for r in data["policies"] if r.get("enabled", True)]

    def test_every_bootstrap_rule_has_a_fixture(self):
        for r in self._bootstrap_rules():
            d = self.FIXTURES / r["id"]
            self.assertTrue((d / "event.json").exists(),
                            msg=f"rule {r['id']} has no fixture event.json")
            self.assertTrue((d / "expected.json").exists(),
                            msg=f"rule {r['id']} has no expected.json")

    def test_every_fixture_lanes_to_its_rule(self):
        policy.ensure_policies_installed()
        rules, _, error = policy.load_policies()
        self.assertIsNone(error)
        for r in self._bootstrap_rules():
            d = self.FIXTURES / r["id"]
            event = json.loads((d / "event.json").read_text())
            expected = json.loads((d / "expected.json").read_text())
            laned = policy.lane_event(event, rules)
            self.assertEqual(laned["lane"], expected["lane"],
                             msg=f"fixture {r['id']}: lane mismatch")
            self.assertEqual(laned["policy_id"], expected["policy_id"],
                             msg=f"fixture {r['id']}: matched wrong rule")

    def test_no_orphan_fixtures(self):
        rule_ids = {r["id"] for r in self._bootstrap_rules()}
        for d in self.FIXTURES.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                self.assertIn(d.name, rule_ids,
                              msg=f"fixture {d.name} has no bootstrap rule")


# ─── miner ───────────────────────────────────────────────────────────────────

def triage_record(dec_id, source="gcal", kind="event_upcoming",
                  suggested="digest", epoch=1783000000):
    return {"schema": "decision/1", "id": dec_id, "epoch": epoch,
            "source": source, "kind": kind, "policy_id": "triage",
            "triage": {"suggested_lane": suggested, "rationale": "fyi"},
            "status": "open"}


class MinerTests(HomeTestCase):
    def _proposals(self):
        p = policy.proposals_path()
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def test_three_identical_suggestions_propose_once(self):
        recs = [triage_record(f"dec-{i:016x}") for i in range(3)]
        written = policy.mine_policy_proposals(recs, now=1783000100, rules=[])
        self.assertEqual(len(written), 1)
        props = self._proposals()
        self.assertEqual(len(props), 1)
        prop = props[0]
        self.assertEqual(prop["type"], "policy")
        self.assertEqual(prop["status"], "pending")
        pp = prop["proposed_policy"]
        self.assertEqual(pp["match"], {"source": "gcal",
                                       "kind": "event_upcoming"})
        self.assertEqual(pp["lane"], "digest")
        self.assertEqual(prop["evidence"]["suggestion_count"], 3)
        # The proposed rule itself must be valid by the engine's own gate.
        self.assertIsNone(policy.validate_rule(pp))

    def test_two_suggestions_do_not_propose(self):
        recs = [triage_record(f"dec-{i:016x}") for i in range(2)]
        self.assertEqual(
            policy.mine_policy_proposals(recs, now=1783000100, rules=[]), [])

    def test_rerun_does_not_duplicate_pending_proposal(self):
        recs = [triage_record(f"dec-{i:016x}") for i in range(3)]
        policy.mine_policy_proposals(recs, now=1783000100, rules=[])
        policy.mine_policy_proposals(recs, now=1783000200, rules=[])
        self.assertEqual(len(self._proposals()), 1)

    def test_disagreeing_lanes_do_not_pool(self):
        recs = [triage_record("dec-" + "a" * 16, suggested="digest"),
                triage_record("dec-" + "b" * 16, suggested="digest"),
                triage_record("dec-" + "c" * 16, suggested="staged")]
        self.assertEqual(
            policy.mine_policy_proposals(recs, now=1783000100, rules=[]), [])

    def test_old_suggestions_outside_window_ignored(self):
        old = 1783000000 - 8 * 86400
        recs = [triage_record(f"dec-{i:016x}", epoch=old) for i in range(3)]
        self.assertEqual(
            policy.mine_policy_proposals(recs, now=1783000000, rules=[]), [])

    def test_already_covered_by_rule_skipped(self):
        recs = [triage_record(f"dec-{i:016x}") for i in range(3)]
        covering = rule("cov", source="gcal", kind="event_upcoming",
                        lane="digest")
        self.assertEqual(policy.mine_policy_proposals(
            recs, now=1783000100, rules=[covering]), [])

    def test_auto_suggestions_never_feed_the_miner(self):
        # Even a hand-forged record claiming suggested_lane=auto can't mint
        # an auto proposal — valid_triage_lane refuses it upstream.
        recs = [triage_record(f"dec-{i:016x}", suggested="auto")
                for i in range(5)]
        self.assertEqual(
            policy.mine_policy_proposals(recs, now=1783000100, rules=[]), [])


if __name__ == "__main__":
    unittest.main()
