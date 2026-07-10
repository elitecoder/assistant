"""Tests for src/assistant/triage.py — the pulse triage step (Keel M2):
mechanical lane actions on policy hits (auto/staged/escalate/digest/drop),
the fail-safe escalate default + suggestion-only LLM batch for unmatched
events, disposition replayability, the delete-safe cursor, escalate-card
mirroring, TTL wiring, and the miner integration.

The LLM is ALWAYS injected here (`llm_batch=`) — no subprocess, no network.
unittest style so the suite runs under `python3 -m unittest discover tests`.
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

from assistant import decisions, policy, triage  # noqa: E402

NOW = 1783598400.0  # 2026-07-09T12:00:00Z


def make_event(i=1, **over) -> dict:
    ev = {
        "schema": "world-event/1",
        "id": f"eid-{i}",
        "ts": "2026-07-09T12:00:00Z",
        "epoch": int(NOW),
        "source": "cmux",
        "kind": "needs_input",
        "external_id": f"cmux:workspace:{i}:needs_input:1:aa",
        "actor": None,
        "title": f"workspace:{i} needs_input",
        "snippet": "approve?",
        "url": None,
        "refs": {"ws_ref": f"workspace:{i}"},
        "raw_path": None,
    }
    ev.update(over)
    return ev


class TriageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        (self.home / ".assistant").mkdir(parents=True)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────

    def write_policies(self, policies):
        # Stamp the bootstrap's own version so the version-upgrade path never
        # rewrites a test's hand-written policy set underneath it.
        version = json.loads(policy.bootstrap_path().read_text())["version"]
        p = policy.policies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": version, "policies": policies}))

    def append_events(self, *events):
        with open(triage.events_path(), "a") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def run_triage(self, llm_batch=None, now=NOW):
        return triage.triage_new_events(pulse_idx=1, now=now,
                                        llm_batch=llm_batch)

    def folded(self):
        return decisions.fold(decisions.read_log())

    def dispositions(self):
        out = []
        for line in triage.events_path().read_text().splitlines():
            d = json.loads(line)
            if d.get("kind") == triage.DISPOSITION_KIND:
                out.append(d)
        return out

    def ledger_rows(self, kind):
        p = self.home / ".assistant/actions-ledger.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        return [r for r in rows if r.get("kind") == kind]

    def rule(self, rid="r1", source="cmux", kind="needs_input",
             lane="escalate", **over):
        r = {"id": rid, "match": {"source": source, "kind": kind},
             "lane": lane, "action": None, "urgency": None, "ttl_h": None,
             "pageable": False, "enabled": True}
        r.update(over)
        return r


# ─── mechanical lanes ────────────────────────────────────────────────────────

class PolicyHitTests(TriageTestCase):
    def test_escalate_hit_opens_decision_and_mirrors_card(self):
        self.write_policies([self.rule(lane="escalate", urgency="now")])
        self.append_events(make_event())
        summary = self.run_triage()
        self.assertEqual(summary["lanes"], {"escalate": 1})
        self.assertEqual(summary["decisions_opened"], 1)
        folded = self.folded()
        self.assertEqual(len(folded), 1)
        dec = next(iter(folded.values()))
        self.assertEqual(dec["status"], "open")
        self.assertEqual(dec["policy_id"], "r1")
        # Card mirror: keyed by the decision id (the purge predicate's key).
        self.assertEqual(len(summary["cards"]), 1)
        self.assertEqual(summary["cards"][0]["key"], dec["id"])
        self.assertEqual(summary["cards"][0]["ws_ref"], "workspace:1")
        # Disposition appended → replayable.
        disps = self.dispositions()
        self.assertEqual(len(disps), 1)
        self.assertEqual(disps[0]["refs"]["event_id"], "eid-1")
        self.assertEqual(disps[0]["lane"], "escalate")
        self.assertEqual(disps[0]["policy_id"], "r1")

    def test_staged_hit_opens_decision_without_card(self):
        self.write_policies([self.rule(lane="staged")])
        self.append_events(make_event())
        summary = self.run_triage()
        self.assertEqual(summary["decisions_opened"], 1)
        self.assertEqual(summary["cards"], [])
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["lane"], "staged")
        self.assertEqual(dec["ttl_h"], 72)  # staged default TTL

    def test_digest_hit_appends_daily_digest_and_open_decision(self):
        self.write_policies([self.rule(lane="digest")])
        self.append_events(make_event())
        summary = self.run_triage()
        self.assertEqual(summary["lanes"], {"digest": 1})
        day = triage.digest_dir() / "2026-07-09.jsonl"
        rows = [json.loads(l) for l in day.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], "eid-1")
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["ttl_h"], 24)
        self.assertEqual(summary["cards"], [])  # digest never cards

    def test_drop_hit_is_a_ledgered_tombstone_only(self):
        self.write_policies([self.rule(lane="drop")])
        self.append_events(make_event())
        summary = self.run_triage()
        self.assertEqual(summary["dropped"], 1)
        self.assertEqual(self.folded(), {})  # no decision for a drop
        rows = self.ledger_rows("event-drop")
        self.assertEqual(len(rows), 1)
        self.assertIn("r1", rows[0]["evidence"])
        self.assertEqual(len(self.dispositions()), 1)

    def test_auto_todo_create_acts_and_records_auto_done(self):
        self.write_policies([self.rule(
            lane="auto",
            action={"class": "todo.create",
                    "params": {"title": "Answer workspace:1",
                               "priority": "P2"}})])
        self.append_events(make_event())
        summary = self.run_triage()
        self.assertEqual(summary["auto_done"], 1)
        todos = json.loads(triage.todo_path().read_text())["items"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["title"], "Answer workspace:1")
        self.assertEqual(todos[0]["source"], "policy:r1:eid-1")
        self.assertFalse(todos[0]["autoDispatch"])  # standing default: manual
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["status"], "auto_done")
        self.assertEqual(dec["policy_id"], "r1")
        self.assertEqual(len(self.ledger_rows("decision-auto-done")), 1)
        self.assertEqual(summary["cards"], [])  # auto_done is not open

    def test_auto_digest_append_acts(self):
        self.write_policies([self.rule(
            lane="auto", action={"class": "digest.append"})])
        self.append_events(make_event())
        summary = self.run_triage()
        self.assertEqual(summary["auto_done"], 1)
        day = triage.digest_dir() / "2026-07-09.jsonl"
        self.assertEqual(len(day.read_text().splitlines()), 1)

    def test_unknown_auto_action_class_escalates_never_acts(self):
        # Second fence: even if a rule with a forbidden class slipped past
        # load validation, _run_auto_action refuses and the event escalates.
        ok, evidence = triage._run_auto_action(
            make_event(), {"action": {"class": "email.send"},
                           "policy_id": "rx"}, NOW)
        self.assertFalse(ok)
        self.assertIn("unknown auto action class", evidence)

    def test_reprocessing_is_idempotent_end_to_end(self):
        # Delete the cursor after a full run: dispositions (in the event log
        # itself) must prevent any double-act — no dup TODO, no dup decision,
        # no dup digest row.
        self.write_policies([
            self.rule("r-auto", lane="auto",
                      action={"class": "todo.create", "params": {}}),
            self.rule("r-dig", kind="work_complete", lane="digest"),
        ])
        self.append_events(make_event(1),
                           make_event(2, kind="work_complete",
                                      external_id="x:2"))
        first = self.run_triage()
        self.assertEqual(first["events_processed"], 2)
        os.unlink(triage.cursor_path())
        second = self.run_triage()
        self.assertEqual(second["events_processed"], 0)
        self.assertEqual(
            len(json.loads(triage.todo_path().read_text())["items"]), 1)
        self.assertEqual(len(self.folded()), 2)
        day = triage.digest_dir() / "2026-07-09.jsonl"
        self.assertEqual(len(day.read_text().splitlines()), 1)


# ─── unmatched → suggestion-only LLM ─────────────────────────────────────────

class UnmatchedTests(TriageTestCase):
    def test_unmatched_gets_failsafe_escalate_decision_before_llm(self):
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))

        def exploding_llm(events):
            raise RuntimeError("LLM down")

        summary = self.run_triage(llm_batch=exploding_llm)
        self.assertEqual(summary["lanes"], {"triage": 1})
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["status"], "open")
        self.assertEqual(dec["lane"], "escalate")   # fail-safe default held
        self.assertEqual(dec["policy_id"], "triage")
        self.assertIsNone(dec["triage"])            # no suggestion landed
        self.assertEqual(len(summary["cards"]), 1)  # escalate → card

    def test_single_llm_call_per_pulse_with_full_batch(self):
        self.write_policies([])
        self.append_events(*(make_event(i, source="github", kind="mystery",
                                        external_id=f"x:{i}")
                             for i in range(1, 6)))
        calls = []

        def llm(events):
            calls.append([e["id"] for e in events])
            return {}

        self.run_triage(llm_batch=llm)
        self.assertEqual(len(calls), 1)             # ONE call per pulse max
        self.assertEqual(len(calls[0]), 5)

    def test_valid_suggestion_lands_on_decision_still_open(self):
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))
        summary = self.run_triage(llm_batch=lambda evs: {
            "eid-1": {"suggested_lane": "digest", "rationale": "just FYI"}})
        self.assertEqual(summary["triage_suggested"], 1)
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["status"], "open")     # suggestions NEVER act
        # Pure annotation: the effective lane stays the fail-safe escalate;
        # the suggestion lives only in dec["triage"].
        self.assertEqual(dec["lane"], "escalate")
        self.assertEqual(dec["triage"]["suggested_lane"], "digest")
        self.assertEqual(dec["triage"]["rationale"], "just FYI")
        disp = self.dispositions()[0]
        self.assertEqual(disp["lane"], "escalate")  # lane of record
        self.assertEqual(disp["policy_id"], "triage")

    def test_suggestion_never_removes_the_escalate_card(self):
        # The reviewers' probe: a staged/digest suggestion used to overwrite
        # the record lane, vanishing the escalate card while the decision
        # stayed open and un-expiring — permanent invisible limbo. The card
        # must survive any suggestion until a human acts.
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))
        summary = self.run_triage(llm_batch=lambda evs: {
            "eid-1": {"suggested_lane": "staged", "rationale": "batch later"}})
        dec = next(iter(self.folded().values()))
        cards = [c for c in summary["cards"] if c["key"] == dec["id"]]
        self.assertEqual(len(cards), 1)
        self.assertIn("triage suggests staged", cards[0]["detail"])
        # And it persists on the NEXT pulse too (the card is re-derived from
        # queue state each pulse, not emitted once).
        summary2 = self.run_triage(now=NOW + 300)
        self.assertTrue(any(c["key"] == dec["id"] for c in summary2["cards"]))

    def test_suggestion_never_changes_ttl(self):
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))
        self.run_triage(llm_batch=lambda evs: {
            "eid-1": {"suggested_lane": "digest", "rationale": "fyi"}})
        dec = next(iter(self.folded().values()))
        self.assertIsNone(dec["ttl_h"])  # escalate never expires by default
        # A year later the decision is still open — the digest suggestion
        # did not smuggle in digest's 24h TTL.
        summary = self.run_triage(now=NOW + 365 * 86400)
        self.assertEqual(summary["expired"], 0)
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["status"], "open")

    def test_digest_suggestion_never_writes_the_digest(self):
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))
        self.run_triage(llm_batch=lambda evs: {
            "eid-1": {"suggested_lane": "digest", "rationale": "fyi"}})
        day_file = triage.digest_dir() / f"{triage.utc_iso(NOW)[:10]}.jsonl"
        self.assertFalse(day_file.exists())

    def test_auto_suggestion_is_structurally_impossible(self):
        # A hostile/buggy LLM answering "auto" (or "drop") is refused by the
        # TRIAGE_LANE_MAP validation: the decision keeps its escalate default
        # and NOTHING acts.
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))
        summary = self.run_triage(llm_batch=lambda evs: {
            "eid-1": {"suggested_lane": "auto", "rationale": "trust me"}})
        self.assertEqual(summary["triage_suggested"], 0)
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["lane"], "escalate")
        self.assertIsNone(dec["triage"])
        self.assertEqual(self.ledger_rows("decision-auto-done"), [])
        self.assertFalse(triage.todo_path().exists())

    def test_no_llm_injected_still_lanes_safely(self):
        self.write_policies([])
        self.append_events(make_event(source="github", kind="mystery"))
        summary = self.run_triage(llm_batch=None)
        self.assertEqual(summary["triage_batch"], 0)
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["lane"], "escalate")

    def test_policy_load_failure_escalates_everything(self):
        p = policy.policies_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{torn")
        self.append_events(make_event())
        called = []
        summary = self.run_triage(llm_batch=lambda evs: called.append(evs))
        # Parse failure is ambiguity → escalate directly; matched-rule laning
        # is unavailable so nothing reaches the LLM path either way.
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["lane"], "escalate")
        self.assertEqual(summary["lanes"], {"escalate": 1})
        self.assertEqual(called, [])


# ─── cursor + scan ───────────────────────────────────────────────────────────

class ScanTests(TriageTestCase):
    def test_cursor_advances_and_second_pulse_sees_only_new(self):
        self.write_policies([self.rule(lane="digest")])
        self.append_events(make_event(1))
        self.assertEqual(self.run_triage()["events_processed"], 1)
        self.append_events(make_event(2, external_id="x:2"))
        summary = self.run_triage()
        self.assertEqual(summary["events_processed"], 1)
        self.assertEqual(len(self.folded()), 2)

    def test_disposition_rows_are_never_treated_as_events(self):
        self.write_policies([self.rule(lane="digest")])
        self.append_events(make_event(1))
        self.run_triage()
        os.unlink(triage.cursor_path())
        events, _ = triage.scan_new_events()
        self.assertEqual(events, [])

    def test_torn_lines_are_skipped_not_fatal(self):
        self.write_policies([self.rule(lane="digest")])
        with open(triage.events_path(), "a") as f:
            f.write("{torn line\n")
        self.append_events(make_event(1))
        self.assertEqual(self.run_triage()["events_processed"], 1)

    def test_missing_events_file_is_a_noop(self):
        self.write_policies([self.rule()])
        summary = self.run_triage()
        self.assertEqual(summary["events_processed"], 0)

    def test_bootstrap_installs_when_no_policies_exist(self):
        self.append_events(make_event())  # cmux needs_input
        summary = self.run_triage()
        self.assertTrue(summary["policy_installed"])
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["policy_id"], "cmux-needs-input-escalate")


# ─── lifecycle wiring: cards, TTL, miner ─────────────────────────────────────

class LifecycleWiringTests(TriageTestCase):
    def test_card_disappears_when_decision_leaves_open(self):
        self.write_policies([self.rule(lane="escalate")])
        self.append_events(make_event())
        summary = self.run_triage()
        dec_id = summary["cards"][0]["key"]
        decisions.transition(dec_id, "accepted", via="test", now=NOW + 10)
        second = self.run_triage(now=NOW + 20)
        self.assertEqual(second["cards"], [])  # derives from queue state

    def test_ttl_sweep_runs_inside_the_step(self):
        self.write_policies([self.rule(lane="digest")])
        self.append_events(make_event())
        self.run_triage(now=NOW)
        summary = self.run_triage(now=NOW + 25 * 3600)
        self.assertEqual(summary["expired"], 1)
        dec = next(iter(self.folded().values()))
        self.assertEqual(dec["status"], "expired")

    def test_three_identical_suggestions_mine_a_policy_proposal(self):
        self.write_policies([])
        self.append_events(*(make_event(i, source="gcal",
                                        kind="event_upcoming",
                                        external_id=f"gcal:{i}")
                             for i in range(1, 4)))
        summary = self.run_triage(llm_batch=lambda evs: {
            e["id"]: {"suggested_lane": "digest", "rationale": "calendar FYI"}
            for e in evs})
        self.assertEqual(summary["triage_suggested"], 3)
        self.assertEqual(summary["proposals"], 1)
        props = [json.loads(l) for l in
                 policy.proposals_path().read_text().splitlines()]
        self.assertEqual(props[0]["type"], "policy")
        self.assertEqual(props[0]["status"], "pending")
        self.assertEqual(props[0]["proposed_policy"]["lane"], "digest")
        # Confirmation-gated: policies.json untouched by the miner.
        rules, _, err = policy.load_policies()
        self.assertIsNone(err)
        self.assertEqual([r["id"] for r in rules], [])

    def test_miner_does_not_repropose_next_pulse(self):
        self.test_three_identical_suggestions_mine_a_policy_proposal()
        summary = self.run_triage(now=NOW + 60)
        self.assertEqual(summary["proposals"], 0)
        props = policy.proposals_path().read_text().splitlines()
        self.assertEqual(len(props), 1)


if __name__ == "__main__":
    unittest.main()
