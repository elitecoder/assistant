"""Tests for src/assistant/decisions.py — the append-only decision queue
(Keel M2): stable ids (re-triage can never enqueue twice), transitions as new
records, the flock'd single-writer append, queue.json as a delete-safe
materialized view (deleted + rebuilt = identical), TTL expiry, and the
transition ledger trail.

unittest style so the suite runs under `python3 -m unittest discover tests`.
Everything runs against a tmp $HOME — decisions.py computes every path per
call.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import decisions  # noqa: E402

NOW = 1783000000.0


def make_event(**over) -> dict:
    ev = {
        "schema": "world-event/1",
        "id": "eid-1",
        "source": "cmux",
        "kind": "needs_input",
        "external_id": "cmux:workspace:7:needs_input:1:aa",
        "title": "workspace:7 needs_input",
        "snippet": "approve?",
        "refs": {"ws_ref": "workspace:7"},
        "raw_path": None,
    }
    ev.update(over)
    return ev


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

    def log_lines(self):
        p = decisions.decisions_path()
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def ledger_rows(self, kind):
        p = self.home / ".assistant/actions-ledger.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        return [r for r in rows if r.get("kind") == kind]


class DecisionIdTests(unittest.TestCase):
    def test_stable_and_deterministic(self):
        a = decisions.decision_id("gmail", "gmail:123", "")
        b = decisions.decision_id("gmail", "gmail:123", "")
        self.assertEqual(a, b)
        self.assertTrue(re.match(r"^dec-[a-f0-9]{16}$", a))

    def test_tuple_members_change_the_id(self):
        base = decisions.decision_id("gmail", "gmail:123", "")
        self.assertNotEqual(base, decisions.decision_id("gmail", "gmail:124", ""))
        self.assertNotEqual(base, decisions.decision_id("gcal", "gmail:123", ""))
        self.assertNotEqual(base, decisions.decision_id("gmail", "gmail:123",
                                                        "todo.create"))


class OpenDecisionTests(HomeTestCase):
    def test_creates_schema_record_and_queue(self):
        rec, created = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="r1",
            urgency="now", now=NOW)
        self.assertTrue(created)
        self.assertEqual(rec["schema"], "decision/1")
        self.assertEqual(rec["status"], "open")
        self.assertEqual(rec["lane"], "escalate")
        self.assertEqual(rec["policy_id"], "r1")
        self.assertEqual(rec["event_ref"], "eid-1")
        self.assertEqual(rec["score"],
                         decisions.score_decision("escalate", "now"))
        self.assertEqual(len(self.log_lines()), 1)
        view = json.loads(decisions.queue_path().read_text())
        self.assertEqual(len(view["decisions"]), 1)
        self.assertEqual(view["decisions"][0]["id"], rec["id"])

    def test_reenqueue_same_event_is_a_noop(self):
        # The M2 dedup contract: same (source, external_id, action_class) →
        # ONE decision, forever. Re-triage returns the existing record.
        rec1, created1 = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="triage", now=NOW)
        rec2, created2 = decisions.open_decision(
            event=make_event(), lane="staged", policy_id="triage", now=NOW + 60)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(rec1["id"], rec2["id"])
        self.assertEqual(rec2["lane"], "escalate")  # original stands
        self.assertEqual(len(self.log_lines()), 1)

    def test_resolved_decision_still_blocks_reenqueue(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="r1", now=NOW)
        decisions.transition(rec["id"], "rejected", via="test", now=NOW + 10)
        rec2, created = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="r1", now=NOW + 20)
        self.assertFalse(created)
        self.assertEqual(rec2["status"], "rejected")

    def test_title_capped_at_120(self):
        rec, _ = decisions.open_decision(
            event=make_event(title="x" * 500), lane="staged", policy_id="r1",
            now=NOW)
        self.assertEqual(len(rec["title"]), 120)

    def test_auto_done_creation_is_ledgered(self):
        decisions.open_decision(
            event=make_event(), lane="auto", policy_id="r-auto",
            action={"class": "todo.create"}, status="auto_done",
            resolution={"ts": "t", "via": "r-auto", "ledger_key": "k"},
            now=NOW)
        rows = self.ledger_rows("decision-auto-done")
        self.assertEqual(len(rows), 1)
        self.assertIn("r-auto", rows[0]["evidence"])


class TransitionTests(HomeTestCase):
    def _open(self, **over):
        rec, _ = decisions.open_decision(
            event=make_event(**over), lane="escalate", policy_id="r1", now=NOW)
        return rec

    def test_transition_appends_a_new_record(self):
        rec = self._open()
        new, err = decisions.transition(rec["id"], "accepted",
                                        via="todo-server:accept", now=NOW + 5)
        self.assertIsNone(err)
        self.assertEqual(new["status"], "accepted")
        self.assertEqual(new["resolution"]["via"], "todo-server:accept")
        # Append-only: the log has BOTH records; the fold shows the latest.
        lines = self.log_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["status"], "open")
        self.assertEqual(lines[1]["status"], "accepted")
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "accepted")

    def test_transition_is_ledgered_from_to(self):
        rec = self._open()
        decisions.transition(rec["id"], "rejected", via="test", now=NOW + 5)
        rows = self.ledger_rows("decision-transition")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], f"decision:{rec['id']}:open->rejected")

    def test_unknown_id_errors(self):
        _, err = decisions.transition("dec-" + "0" * 16, "accepted", via="t")
        self.assertIn("not found", err)

    def test_resolved_decision_cannot_transition_again(self):
        rec = self._open()
        decisions.transition(rec["id"], "accepted", via="t", now=NOW + 5)
        _, err = decisions.transition(rec["id"], "rejected", via="t",
                                      now=NOW + 10)
        self.assertIn("only open/snoozed", err)

    def test_invalid_target_status_errors(self):
        rec = self._open()
        for bad in ("open", "auto_done", "yolo"):
            _, err = decisions.transition(rec["id"], bad, via="t")
            self.assertIsNotNone(err, msg=bad)

    def test_snooze_records_wake_ts_and_can_still_resolve(self):
        rec = self._open()
        new, err = decisions.transition(rec["id"], "snoozed", via="t",
                                        wake_ts=NOW + 3600, now=NOW + 5)
        self.assertIsNone(err)
        self.assertEqual(new["wake_ts"], int(NOW + 3600))
        _, err = decisions.transition(rec["id"], "accepted", via="t",
                                      now=NOW + 10)
        self.assertIsNone(err)


class AnnotateTriageTests(HomeTestCase):
    def test_suggestion_lands_without_touching_status(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="triage", now=NOW)
        new = decisions.annotate_triage(rec["id"], "digest", "just FYI",
                                        now=NOW + 5)
        self.assertEqual(new["status"], "open")       # suggestions never act
        self.assertEqual(new["triage"]["suggested_lane"], "digest")
        self.assertEqual(new["epoch"], rec["epoch"])  # creation epoch kept

    def test_suggestion_is_a_pure_annotation(self):
        # Design section 5: suggestions land on the record, never act. The
        # effective lane, TTL, and score stay policy-derived (escalate) — a
        # digest suggestion must not pull the decision off the fail-safe
        # escalate surface, change when it expires, or re-rank it.
        rec, _ = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="triage",
            ttl_h=None, now=NOW)
        new = decisions.annotate_triage(rec["id"], "digest", "just FYI",
                                        now=NOW + 5)
        self.assertEqual(new["lane"], "escalate")
        self.assertIsNone(new["ttl_h"])
        self.assertEqual(new["score"], rec["score"])
        # The annotated decision still never TTL-expires (escalate ttl is
        # None) — the digest suggestion must not smuggle in digest's 24h.
        self.assertEqual(decisions.expire_open(now=NOW + 365 * 86400), [])

    def test_resolved_decision_not_annotated(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="triage", now=NOW)
        decisions.transition(rec["id"], "accepted", via="t", now=NOW + 1)
        self.assertIsNone(
            decisions.annotate_triage(rec["id"], "digest", "x", now=NOW + 2))


class ExpiryTests(HomeTestCase):
    def test_digest_expires_after_ttl(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="digest", policy_id="r1", ttl_h=24,
            now=NOW)
        expired = decisions.expire_open(now=NOW + 25 * 3600)
        self.assertEqual([r["id"] for r in expired], [rec["id"]])
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "expired")
        self.assertEqual(folded[rec["id"]]["resolution"]["via"], "ttl")

    def test_escalate_without_ttl_never_expires(self):
        decisions.open_decision(
            event=make_event(), lane="escalate", policy_id="r1", ttl_h=None,
            now=NOW)
        self.assertEqual(decisions.expire_open(now=NOW + 365 * 86400), [])

    def test_snoozed_decision_wakes_back_to_open(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="staged", policy_id="r1", ttl_h=None,
            now=NOW)
        decisions.transition(rec["id"], "snoozed", via="t",
                             wake_ts=NOW + 600, now=NOW + 5)
        decisions.expire_open(now=NOW + 601)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "open")

    def test_ttl_measured_from_creation_not_last_touch(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="digest", policy_id="triage", ttl_h=24,
            now=NOW)
        # A triage annotation 23h in must not reset the clock.
        decisions.annotate_triage(rec["id"], "digest", "fyi",
                                  now=NOW + 23 * 3600)
        expired = decisions.expire_open(now=NOW + 25 * 3600)
        self.assertEqual(len(expired), 1)


class QueueViewTests(HomeTestCase):
    def _seed(self):
        a, _ = decisions.open_decision(
            event=make_event(id="e-a", external_id="x:a"), lane="digest",
            policy_id="r1", urgency="low", ttl_h=24, now=NOW)
        b, _ = decisions.open_decision(
            event=make_event(id="e-b", external_id="x:b"), lane="escalate",
            policy_id="r2", urgency="now", now=NOW + 1)
        c, _ = decisions.open_decision(
            event=make_event(id="e-c", external_id="x:c"), lane="staged",
            policy_id="r3", now=NOW + 2)
        decisions.transition(c["id"], "accepted", via="t", now=NOW + 3)
        return a, b, c

    def test_open_sorted_by_score_resolved_last(self):
        a, b, c = self._seed()
        view = decisions.load_queue()
        ids = [d["id"] for d in view["decisions"]]
        self.assertEqual(ids, [b["id"], a["id"], c["id"]])
        self.assertEqual([d["id"] for d in decisions.open_decisions(view)],
                         [b["id"], a["id"]])

    def test_queue_is_delete_safe_rebuilt_identical(self):
        # THE delete-and-rebuild-diff test the milestone requires: nuke the
        # materialized view, rebuild from the log, byte-compare the decisions
        # payload (ts is the rebuild stamp and is excluded by design).
        self._seed()
        before = json.loads(decisions.queue_path().read_text())["decisions"]
        os.unlink(decisions.queue_path())
        self.assertFalse(decisions.queue_path().exists())
        after = decisions.load_queue()["decisions"]
        self.assertEqual(json.dumps(before, sort_keys=True),
                         json.dumps(after, sort_keys=True))
        self.assertTrue(decisions.queue_path().exists())

    def test_corrupt_queue_json_is_rebuilt(self):
        self._seed()
        decisions.queue_path().write_text("{torn")
        view = decisions.load_queue()
        self.assertEqual(len(view["decisions"]), 3)

    def test_corrupt_log_lines_are_skipped(self):
        self._seed()
        with open(decisions.decisions_path(), "a") as f:
            f.write("{torn json\n")
        self.assertEqual(len(decisions.rebuild_queue()["decisions"]), 3)


class ConcurrencyTests(HomeTestCase):
    def test_concurrent_transitions_exactly_one_wins(self):
        # The check-then-append race: todo-server accept vs pulse expire.
        # With read-fold-guard-append inside ONE flock'd critical section,
        # exactly one transition lands; the loser gets a refused result.
        import threading
        rec, _ = decisions.open_decision(
            event=make_event(), lane="staged", policy_id="r1", ttl_h=72,
            now=NOW)
        barrier = threading.Barrier(2)
        results = {}

        def act(name, to_status):
            barrier.wait()
            results[name] = decisions.transition(
                rec["id"], to_status, via=name, now=NOW + 10)

        t1 = threading.Thread(target=act, args=("accept", "accepted"))
        t2 = threading.Thread(target=act, args=("expire", "expired"))
        t1.start(); t2.start(); t1.join(); t2.join()
        outcomes = {name: (r, err) for name, (r, err) in results.items()}
        winners = [n for n, (r, err) in outcomes.items() if err is None]
        losers = [n for n, (r, err) in outcomes.items() if err is not None]
        self.assertEqual(len(winners), 1, msg=str(outcomes))
        self.assertEqual(len(losers), 1, msg=str(outcomes))
        # The loser was refused BECAUSE the winner's state landed first.
        _, err = outcomes[losers[0]]
        self.assertIn("only open/snoozed can transition", err)
        # And the log holds exactly one transition record.
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(len(decisions.read_log()), 2)  # create + one flip
        self.assertIn(folded[rec["id"]]["status"], ("accepted", "expired"))


class FoldOrderTests(HomeTestCase):
    def test_fold_orders_by_epoch_with_file_order_tiebreak(self):
        # A record appended out of epoch order (hand merge, clock skew) must
        # not roll state back: the highest epoch wins; equal epochs fall back
        # to file order (last write wins).
        rec, _ = decisions.open_decision(
            event=make_event(), lane="staged", policy_id="r1", now=NOW)
        decisions.transition(rec["id"], "accepted", via="t", now=NOW + 100)
        stale = dict(rec)
        stale["ts"] = decisions.utc_iso(NOW + 50)
        stale["epoch"] = int(NOW + 50)   # older than the accepted record
        with open(decisions.decisions_path(), "a") as f:
            f.write(json.dumps(stale) + "\n")
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "accepted")
        # Equal epochs: file order breaks the tie (later line wins).
        dup = dict(stale)
        dup["epoch"] = int(NOW + 100)
        dup["status"] = "rejected"
        with open(decisions.decisions_path(), "a") as f:
            f.write(json.dumps(dup) + "\n")
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "rejected")


class ReopenTests(HomeTestCase):
    def test_expired_decision_reopens_on_new_sighting(self):
        rec, _ = decisions.open_decision(
            event=make_event(ts=decisions.utc_iso(NOW)), lane="digest",
            policy_id="r1", ttl_h=24, now=NOW)
        decisions.expire_open(now=NOW + 25 * 3600)
        # A NEW sighting of the same stable id (event ts > resolution ts).
        later = NOW + 30 * 3600
        rec2, created = decisions.open_decision(
            event=make_event(ts=decisions.utc_iso(later)), lane="digest",
            policy_id="r1", ttl_h=24, now=later)
        self.assertTrue(created)
        self.assertEqual(rec2["id"], rec["id"])
        self.assertEqual(rec2["status"], "open")
        rows = self.ledger_rows("decision-transition")
        self.assertTrue(any("expired->open" in r.get("key", "")
                            for r in rows))

    def test_expired_decision_stays_dead_for_old_sighting(self):
        rec, _ = decisions.open_decision(
            event=make_event(ts=decisions.utc_iso(NOW)), lane="digest",
            policy_id="r1", ttl_h=24, now=NOW)
        decisions.expire_open(now=NOW + 25 * 3600)
        # Re-triaging the ORIGINAL event (ts before the expiry) is a noop.
        rec2, created = decisions.open_decision(
            event=make_event(ts=decisions.utc_iso(NOW)), lane="digest",
            policy_id="r1", ttl_h=24, now=NOW + 26 * 3600)
        self.assertFalse(created)
        self.assertEqual(rec2["status"], "expired")

    def test_rejected_decision_never_reopens(self):
        rec, _ = decisions.open_decision(
            event=make_event(ts=decisions.utc_iso(NOW)), lane="staged",
            policy_id="r1", now=NOW)
        decisions.transition(rec["id"], "rejected", via="t", now=NOW + 10)
        rec2, created = decisions.open_decision(
            event=make_event(ts=decisions.utc_iso(NOW + 86400)), lane="staged",
            policy_id="r1", now=NOW + 86400)
        self.assertFalse(created)
        self.assertEqual(rec2["status"], "rejected")


class WakeLedgerTests(HomeTestCase):
    def test_snooze_wake_gets_a_ledger_row(self):
        rec, _ = decisions.open_decision(
            event=make_event(), lane="staged", policy_id="r1", ttl_h=None,
            now=NOW)
        decisions.transition(rec["id"], "snoozed", via="t",
                             wake_ts=NOW + 600, now=NOW + 5)
        decisions.expire_open(now=NOW + 601)
        rows = self.ledger_rows("decision-transition")
        self.assertTrue(any("snoozed->open" in r.get("key", "")
                            for r in rows), msg=str(rows))


class BatchExpiryTests(HomeTestCase):
    def test_batch_expiry_does_one_queue_rebuild(self):
        from unittest import mock
        for i in range(5):
            decisions.open_decision(
                event=make_event(id=f"e-{i}", external_id=f"x:{i}"),
                lane="digest", policy_id="r1", ttl_h=24, now=NOW)
        with mock.patch.object(decisions, "_write_queue",
                               wraps=decisions._write_queue) as wq:
            expired = decisions.expire_open(now=NOW + 25 * 3600)
        self.assertEqual(len(expired), 5)
        self.assertEqual(wq.call_count, 1)  # one rebuild for the whole sweep
        folded = decisions.fold(decisions.read_log())
        self.assertTrue(all(folded[r["id"]]["status"] == "expired"
                            for r in expired))


class CompactionTests(HomeTestCase):
    def test_compaction_preserves_fold_equivalence(self):
        recs = []
        for i in range(6):
            rec, _ = decisions.open_decision(
                event=make_event(id=f"e-{i}", external_id=f"x:{i}"),
                lane="staged", policy_id="r1", ttl_h=72, now=NOW + i)
            recs.append(rec)
        decisions.transition(recs[0]["id"], "accepted", via="t", now=NOW + 10)
        decisions.transition(recs[1]["id"], "rejected", via="t", now=NOW + 11)
        before = decisions.fold(decisions.read_log())
        # Force compaction on the next append by shrinking the threshold.
        old_max = decisions.MAX_LOG_BYTES
        decisions.MAX_LOG_BYTES = 1
        try:
            rec, _ = decisions.open_decision(
                event=make_event(id="e-new", external_id="x:new"),
                lane="digest", policy_id="r1", ttl_h=24, now=NOW + 20)
        finally:
            decisions.MAX_LOG_BYTES = old_max
        after = decisions.fold(decisions.read_log())
        # fold(before) ⊂ fold(after): every pre-compaction id folds to the
        # identical record (terminal states preserved); the new append is
        # the only addition.
        for dec_id, want in before.items():
            self.assertEqual(json.dumps(after[dec_id], sort_keys=True),
                             json.dumps(want, sort_keys=True))
        self.assertEqual(set(after) - set(before), {rec["id"]})
        # One line per id in the compacted log (history rotated to .1).
        log_ids = [r["id"] for r in self.log_lines()]
        self.assertEqual(len(log_ids), len(set(log_ids)))
        rotated = decisions.decisions_path().with_name(
            decisions.decisions_path().name + ".1")
        self.assertTrue(rotated.exists())


class CreatedEpochSurvivesCompactionTests(HomeTestCase):
    """F15: log compaction preserves each decision's ORIGINAL creation epoch,
    so ages and TTLs are unaffected by compaction (the create record is gone
    but created_epoch carries the birth time)."""

    def test_ttl_measured_from_creation_after_compaction(self):
        rec, _ = decisions.open_decision(
            event=make_event(id="e-x", external_id="x:x"),
            lane="staged", policy_id="r1", ttl_h=72, now=NOW)
        # A snooze→wake cycle advances the latest record's `epoch` well past
        # creation, so a naive "epoch == creation" read would be wrong.
        decisions.transition(rec["id"], "snoozed", via="t",
                             wake_ts=NOW + 10, now=NOW + 5)
        decisions.expire_open(now=NOW + 10)  # wakes it → open, epoch=NOW+10
        # Force compaction, discarding the create record.
        old_max = decisions.MAX_LOG_BYTES
        decisions.MAX_LOG_BYTES = 1
        try:
            decisions.open_decision(
                event=make_event(id="e-pad", external_id="x:pad"),
                lane="digest", policy_id="r1", now=NOW + 20)
        finally:
            decisions.MAX_LOG_BYTES = old_max
        # The surviving folded record keeps the true creation epoch …
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["created_epoch"], int(NOW))
        # … so the TTL fires 72h after CREATION, not 72h after the wake. At
        # 72h+5s the decision has genuinely aged out; a from-transition read
        # (NOW+10) would still consider it fresh here — this instant is the
        # discriminator that proves the fix.
        decisions.expire_open(now=NOW + 72 * 3600 + 5)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "expired")


class LanePartitionOrderingTests(HomeTestCase):
    """F6: the queue partitions by lane so escalate is always on top, even
    when a lower lane's urgency would out-score it."""

    def test_escalate_low_outranks_staged_now(self):
        s, _ = decisions.open_decision(
            event=make_event(id="e-s", external_id="x:s"),
            lane="staged", policy_id="r1", urgency="now", now=NOW)
        e, _ = decisions.open_decision(
            event=make_event(id="e-e", external_id="x:e"),
            lane="escalate", policy_id="r2", urgency="low", now=NOW + 1)
        # staged+now scores 110, escalate+low scores 100 — but the partition
        # keeps escalate at the top of the open queue.
        self.assertGreater(decisions.score_decision("staged", "now"),
                           decisions.score_decision("escalate", "low"))
        view = decisions.load_queue()
        open_lanes = [d["lane"] for d in decisions.open_decisions(view)]
        self.assertEqual(open_lanes[0], "escalate")


class ScoreTests(unittest.TestCase):
    def test_deterministic_ordering_weights(self):
        self.assertGreater(decisions.score_decision("escalate", "now"),
                           decisions.score_decision("escalate", None))
        self.assertGreater(decisions.score_decision("escalate", "now"),
                           decisions.score_decision("staged", "now"))
        self.assertGreater(decisions.score_decision("staged", None),
                           decisions.score_decision("digest", None))
        self.assertEqual(decisions.score_decision("escalate", "now"), 150)


if __name__ == "__main__":
    unittest.main()
