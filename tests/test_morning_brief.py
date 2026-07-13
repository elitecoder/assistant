"""Tests for src/assistant/brief.py (Keel M3): the deterministic scorer's
config weights, the four brief sections, graceful degradation on absent
stores (goals don't exist until M4), the delete-and-rebuild byte-diff, the
seen sidecar, the >48h-unseen degradation pass (TTL to digest + miner feed),
the once-per-date north-star metrics row, and the pulse step's wake-hour
gating.

Everything runs under a tmp $HOME (brief/decisions/policy/triage compute
paths per call). NOW is anchored to a LOCAL morning-hour instant via
datetime(...).timestamp() so the wake-hour tests are timezone-independent.

Named test_morning_brief (not test_brief) deliberately: unittest discovery
imports modules alphabetically, and a module sorting before test_daemon that
puts src/ on sys.path changes whether test_daemon (which imports `assistant`
with no path insert of its own) loads — this suite must not alter that
pre-existing behavior.
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

from assistant import brief, decisions, triage  # noqa: E402

# 2026-07-02 10:00 LOCAL — after the default wake_hour on every machine tz.
NOW = datetime(2026, 7, 2, 10, 0).timestamp()


def make_event(i=1, source="cmux", kind="needs_input", **over) -> dict:
    ev = {"schema": "world-event/1", "id": f"eid-{i}", "source": source,
          "kind": kind, "external_id": f"{source}:{kind}:{i}",
          "title": f"event {i} title", "snippet": f"snippet {i}",
          "refs": {"ws_ref": f"workspace:{i}"}, "raw_path": None,
          "ts": brief.utc_iso(NOW)}
    ev.update(over)
    return ev


class BriefBase(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()


class ScorerTests(BriefBase):
    """The ranking is a pure function of SCORE_CONFIG — no LLM ordering."""

    def test_lane_base_dominates(self):
        esc = {"lane": "escalate", "urgency": None}
        stg = {"lane": "staged", "urgency": None}
        dig = {"lane": "digest", "urgency": None}
        self.assertGreater(brief.brief_score(esc, NOW, NOW),
                           brief.brief_score(stg, NOW, NOW))
        self.assertGreater(brief.brief_score(stg, NOW, NOW),
                           brief.brief_score(dig, NOW, NOW))

    def test_urgency_bump(self):
        base = brief.brief_score({"lane": "staged", "urgency": None}, NOW, NOW)
        now_u = brief.brief_score({"lane": "staged", "urgency": "now"}, NOW, NOW)
        self.assertEqual(now_u - base, brief.SCORE_CONFIG["urgency"]["now"])

    def test_age_decay_freshness_term(self):
        rec = {"lane": "staged", "urgency": None}
        fresh = brief.brief_score(rec, NOW, NOW)
        one_day = brief.brief_score(rec, NOW - 86400, NOW)
        ancient = brief.brief_score(rec, NOW - 30 * 86400, NOW)
        cfg = brief.SCORE_CONFIG
        self.assertEqual(fresh - one_day, cfg["age_decay_per_day"])
        # Decay bottoms out at 0 — an ancient decision keeps its lane base.
        self.assertEqual(ancient, cfg["lane_base"]["staged"])

    def test_decay_cap_never_crosses_lane_bands(self):
        """A brand-new digest decision can never outrank an old staged one:
        the freshness cap is below the 40-point lane gaps."""
        cfg = brief.SCORE_CONFIG
        self.assertLess(cfg["age_decay_cap"],
                        cfg["lane_base"]["staged"] - cfg["lane_base"]["digest"])
        new_digest = brief.brief_score({"lane": "digest", "urgency": None},
                                       NOW, NOW)
        old_staged = brief.brief_score({"lane": "staged", "urgency": None},
                                       NOW - 30 * 86400, NOW)
        self.assertLess(new_digest, old_staged)

    def test_weights_come_from_the_config_dict(self):
        custom = {"lane_base": {"staged": 7}, "urgency": {}, "goal_boost": 3,
                  "age_decay_per_day": 0.0, "age_decay_cap": 0.0}
        got = brief.brief_score({"lane": "staged", "urgency": None},
                                NOW, NOW, config=custom)
        self.assertEqual(got, 10)


class BuildBriefTests(BriefBase):
    def seed(self):
        decisions.open_decision(event=make_event(1), lane="escalate",
                                policy_id="rule-esc", urgency="now", now=NOW - 3600)
        decisions.open_decision(event=make_event(2), lane="staged",
                                policy_id="rule-staged", ttl_h=72, now=NOW - 7200)
        # auto_done → a receipt on the ledger, NOT a queue row.
        decisions.open_decision(
            event=make_event(3, kind="work_complete"), lane="auto",
            policy_id="rule-auto",
            action={"class": "digest.append"}, status="auto_done",
            resolution={"ts": brief.utc_iso(NOW - 1800), "via": "rule-auto",
                        "ledger_key": "k"},
            now=NOW - 1800)
        triage.append_digest(make_event(4, kind="crash_event"),
                             "rule-digest", now=NOW - 900)
        # world.json events section (M1 shape).
        world = self.home / ".claude/cache/world.json"
        world.parent.mkdir(parents=True, exist_ok=True)
        world.write_text(json.dumps({
            "_meta": {"built_at": brief.utc_iso(NOW - 60)},
            "events": {"total_24h": 9, "quarantine_pending": 1,
                       "by_source": {"cmux": {"count_24h": 9,
                                              "latest_ts": brief.utc_iso(NOW - 120),
                                              "latest_age_sec": 120}}},
        }))
        # An interrupt denial on the ledger (the gate's audit row).
        with open(decisions.ledger_path(), "a") as f:
            f.write(json.dumps({
                "ts": brief.utc_iso(NOW - 600), "epoch": int(NOW - 600),
                "key": "interrupt:denied:notify:k", "kind": "interrupt-denied",
                "ws_ref": "(interrupt-gate)", "outcome": "skipped",
                "evidence": "notify denied: budget exhausted (0/0)"}) + "\n")

    def test_four_sections_populated(self):
        self.seed()
        doc = brief.build_brief(now=NOW)
        self.assertEqual(doc["schema"], "morning-brief/1")
        # 1. queue: two open decisions, escalate ranked first, provenance +
        #    default action on every row.
        self.assertEqual(len(doc["queue"]), 2)
        self.assertEqual(doc["queue"][0]["lane"], "escalate")
        self.assertEqual(doc["queue"][0]["policy_id"], "rule-esc")
        self.assertEqual(doc["queue"][0]["default_action"], "accept")
        self.assertTrue(doc["queue"][0]["title"])
        self.assertGreater(doc["queue"][0]["score"], doc["queue"][1]["score"])
        # 2. receipts: the auto_done row with its rule id.
        kinds = [r["kind"] for r in doc["handled_overnight"]]
        self.assertIn("decision-auto-done", kinds)
        auto_row = next(r for r in doc["handled_overnight"]
                        if r["kind"] == "decision-auto-done")
        self.assertIn("rule-auto", auto_row["evidence"])
        # 3. digest grouped by source.
        self.assertIn("cmux", doc["digest"])
        self.assertEqual(doc["digest"]["cmux"][0]["policy_id"], "rule-digest")
        # 4. health: staleness, interrupts, cost, expired-unseen.
        h = doc["health"]
        self.assertEqual(h["event_sources"]["cmux"]["latest_age_sec"], 120)
        self.assertEqual(h["quarantine_pending"], 1)
        self.assertEqual(h["interrupts"]["denied_24h"], 1)
        self.assertEqual(h["interrupts"]["delivered_24h"], 0)
        self.assertEqual(h["interrupts"]["budget"], {"page": 0, "notify": 0})
        self.assertIn("cost_per_day_usd", h["cost"])
        self.assertEqual(h["expired_unseen_24h"], 0)
        # Goals degrade gracefully until M4.
        self.assertFalse(doc["goals"]["available"])

    def test_empty_home_degrades_gracefully(self):
        doc = brief.build_brief(now=NOW)
        self.assertEqual(doc["queue"], [])
        self.assertEqual(doc["handled_overnight"], [])
        self.assertEqual(doc["digest"], {})
        self.assertEqual(doc["counts"]["open_decisions"], 0)
        self.assertFalse(doc["goals"]["available"])

    def test_delete_and_rebuild_is_byte_identical(self):
        """The brief is a pure derivation: delete the file, rebuild for the
        same instant, and the bytes match (design: delete-safe stores)."""
        self.seed()
        path = brief.write_brief(brief.build_brief(now=NOW))
        first = path.read_bytes()
        path.unlink()
        self.assertFalse(path.exists())
        path2 = brief.write_brief(brief.build_brief(now=NOW))
        self.assertEqual(path, path2)
        self.assertEqual(first, path2.read_bytes())

    def test_expired_unseen_counts_exclude_digest_lane(self):
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", ttl_h=1, now=NOW - 7200)
        decisions.open_decision(event=make_event(2), lane="digest",
                                policy_id="p", ttl_h=1, now=NOW - 7200)
        decisions.expire_open(now=NOW - 60)  # both TTL out
        records = decisions.read_log()
        self.assertEqual(brief.expired_unseen_count(records, NOW), 1)


class SeenAndMetricsTests(BriefBase):
    def test_mark_seen_writes_sidecar_not_brief(self):
        path = brief.write_brief(brief.build_brief(now=NOW))
        before = path.read_bytes()
        ok, msg = brief.mark_seen(now=NOW + 60)
        self.assertTrue(ok, msg)
        sidecar = brief.seen_path(brief.local_date(NOW))
        self.assertTrue(sidecar.exists())
        self.assertEqual(json.loads(sidecar.read_text())["seen_ts"],
                         brief.utc_iso(NOW + 60))
        # The brief file itself is untouched — it stays a pure derivation.
        self.assertEqual(path.read_bytes(), before)
        # Second view is a no-op.
        ok2, msg2 = brief.mark_seen(now=NOW + 120)
        self.assertTrue(ok2)
        self.assertIn("already seen", msg2)

    def test_mark_seen_without_brief_fails(self):
        ok, msg = brief.mark_seen()
        self.assertFalse(ok)
        self.assertIn("no brief", msg)

    def test_metrics_row_appends_once_per_date(self):
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", now=NOW - 3600)
        doc = brief.build_brief(now=NOW)
        row = brief.append_daily_metrics(doc, now=NOW)
        self.assertIsNotNone(row)
        self.assertEqual(
            sorted(row), sorted([
                "date", "ts", "epoch", "decisions_pending_at_brief",
                "decisions_accepted_unedited", "auto_coverage_pct",
                "expired_unseen", "interrupts_delivered",
                "interrupts_denied",
                # Keel M4 extended the row (design section 3):
                "goals_progressed_overnight", "staged_accept_rate"]))
        self.assertEqual(row["decisions_pending_at_brief"], 1)
        # Rebuild same date → no double-booking.
        self.assertIsNone(brief.append_daily_metrics(doc, now=NOW + 60))
        self.assertEqual(len(brief.read_daily_metrics()), 1)

    def test_metrics_auto_coverage_and_accepted(self):
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", now=NOW - 3600)
        decisions.open_decision(
            event=make_event(2), lane="auto", policy_id="rule-auto",
            action={"class": "digest.append"}, status="auto_done",
            resolution={"ts": brief.utc_iso(NOW - 3600), "via": "rule-auto",
                        "ledger_key": "k"}, now=NOW - 3600)
        rec, _ = decisions.open_decision(event=make_event(3), lane="staged",
                                         policy_id="p", now=NOW - 3000)
        decisions.transition(rec["id"], "accepted", via="test", now=NOW - 60)
        doc = brief.build_brief(now=NOW)
        row = brief.compute_daily_metrics(doc, now=NOW)
        self.assertEqual(row["decisions_accepted_unedited"], 1)
        # 3 decisions created in 24h, 1 terminated auto_done.
        self.assertAlmostEqual(row["auto_coverage_pct"], 33.3, places=1)


class DegradeUnseenTests(BriefBase):
    def seed_and_write_brief(self, n_staged=3):
        ids = []
        for i in range(1, n_staged + 1):
            rec, _ = decisions.open_decision(
                event=make_event(i, kind="stale_kind"), lane="staged",
                policy_id="rule-staged", now=NOW - 3600)
            ids.append(rec["id"])
        esc, _ = decisions.open_decision(
            event=make_event(99, kind="needs_input"), lane="escalate",
            policy_id="rule-esc", now=NOW - 3600)
        brief.write_brief(brief.build_brief(now=NOW))
        return ids, esc["id"]

    def test_unseen_brief_ttls_non_escalate_to_digest_and_mines(self):
        staged_ids, esc_id = self.seed_and_write_brief()
        later = NOW + 49 * 3600  # past the 48h unseen TTL
        out = brief.degrade_unseen(now=later)
        self.assertEqual(out["briefs_unseen"], 1)
        self.assertEqual(sorted(out["expired"]), sorted(staged_ids))
        folded = decisions.fold(decisions.read_log())
        for dec_id in staged_ids:
            self.assertEqual(folded[dec_id]["status"], "expired")
            self.assertEqual(folded[dec_id]["resolution"]["via"],
                             "brief-unseen")
        # Escalate is NEVER degraded — the fail-safe lane survives neglect.
        self.assertEqual(folded[esc_id]["status"], "open")
        # Each degraded decision landed in today's digest.
        day_file = triage.digest_dir() / f"{brief.utc_iso(later)[:10]}.jsonl"
        rows = [json.loads(l) for l in day_file.read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["policy_id"] == "brief-unseen" for r in rows))
        # ≥3 unseen expiries for one (source, kind) → ONE digest-lane
        # policy proposal (the pressure valve is rules, not taps).
        self.assertEqual(out["proposals"], 1)
        props = [json.loads(l) for l in
                 (self.home / ".assistant/comms/proposals.jsonl")
                 .read_text().splitlines()]
        self.assertEqual(props[0]["type"], "policy")
        self.assertEqual(props[0]["source"], "unseen-miner")
        self.assertEqual(props[0]["proposed_policy"]["lane"], "digest")
        self.assertEqual(props[0]["proposed_policy"]["match"],
                         {"source": "cmux", "kind": "stale_kind"})
        self.assertEqual(props[0]["status"], "pending")

    def test_seen_brief_is_never_degraded(self):
        staged_ids, _ = self.seed_and_write_brief()
        brief.mark_seen(now=NOW + 3600)
        out = brief.degrade_unseen(now=NOW + 49 * 3600)
        self.assertEqual(out["briefs_unseen"], 0)
        folded = decisions.fold(decisions.read_log())
        self.assertTrue(all(folded[d]["status"] == "open"
                            for d in staged_ids))

    def test_fresh_brief_is_not_degraded(self):
        staged_ids, _ = self.seed_and_write_brief()
        out = brief.degrade_unseen(now=NOW + 3600)
        self.assertEqual(out["briefs_unseen"], 0)

    def test_degradation_is_idempotent(self):
        self.seed_and_write_brief()
        later = NOW + 49 * 3600
        first = brief.degrade_unseen(now=later)
        second = brief.degrade_unseen(now=later + 60)
        self.assertEqual(len(first["expired"]), 3)
        self.assertEqual(second["expired"], [])  # already expired → no-op
        self.assertEqual(second["proposals"], 0)  # pending proposal dedups


class PulseStepTests(BriefBase):
    def test_before_wake_hour_no_build(self):
        early = datetime(2026, 7, 2, 6, 30).timestamp()
        out = brief.pulse_step(now=early)
        self.assertFalse(out["built"])
        self.assertIsNone(brief.latest_brief_date())

    def test_first_pulse_after_wake_builds_then_noops(self):
        out = brief.pulse_step(now=NOW)
        self.assertTrue(out["built"])
        self.assertTrue(brief.brief_path(brief.local_date(NOW)).exists())
        self.assertTrue(brief.metrics_path().exists())
        again = brief.pulse_step(now=NOW + 300)
        self.assertFalse(again["built"])

    def test_wake_hour_is_configurable(self):
        cfg = self.home / ".assistant/comms/config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"brief": {"wake_hour": 11}}))
        self.assertEqual(brief.wake_hour(), 11)
        out = brief.pulse_step(now=NOW)  # 10:00 local < 11
        self.assertFalse(out["built"])
        out2 = brief.pulse_step(now=datetime(2026, 7, 2, 11, 5).timestamp())
        self.assertTrue(out2["built"])

    def test_mangled_config_falls_back_to_default(self):
        cfg = self.home / ".assistant/comms/config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{not json")
        self.assertEqual(brief.wake_hour(), brief.DEFAULT_WAKE_HOUR)


# ─── adversarial-review regressions (Keel M3 fix cycle) ──────────────────────

class BadTypedRowTests(BriefBase):
    """F3: a parseable-but-wrong-typed decisions.jsonl row (ISO-string epoch,
    unhashable list/dict lane/urgency) must NOT crash build_brief /
    compute_daily_metrics — one bad line never blanks the whole brief."""

    def _write_raw(self, records):
        p = decisions.decisions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_bad_epoch_and_unhashable_lane_do_not_blank_brief(self):
        # A good decision that must still surface …
        good, _ = decisions.open_decision(
            event=make_event(1), lane="escalate", policy_id="p",
            urgency="now", now=NOW - 3600)
        # … alongside two poisoned rows.
        self._write_raw([
            {"schema": "decision/1", "id": "dec-badepoch",
             "epoch": "2026-07-02T10:00:00Z", "status": "open",
             "lane": "staged", "urgency": "now", "title": "iso epoch"},
            {"schema": "decision/1", "id": "dec-badlane",
             "epoch": int(NOW - 1800), "status": "open",
             "lane": ["not", "a", "str"], "urgency": {"d": 1},
             "title": "unhashable lane"},
        ])
        doc = brief.build_brief(now=NOW)          # must not raise
        ids = [r["id"] for r in doc["queue"]]
        self.assertIn(good["id"], ids)            # good row survives
        self.assertEqual(doc["queue"][0]["lane"], "escalate")  # partition holds
        row = brief.compute_daily_metrics(doc, now=NOW)  # must not raise
        self.assertIn("decisions_pending_at_brief", row)


class NorthStarMetricTests(BriefBase):
    """F14: decisions_pending_at_brief excludes the digest lane, and
    decisions_accepted_unedited is a RATIO, not a raw count."""

    def test_digest_lane_excluded_from_pending(self):
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", now=NOW - 3600)
        decisions.open_decision(event=make_event(2), lane="digest",
                                policy_id="p", ttl_h=24, now=NOW - 3600)
        doc = brief.build_brief(now=NOW)
        # Both are OPEN and appear in the rendered queue …
        self.assertEqual(len(doc["queue"]), 2)
        # … but the north star counts only the actionable (non-digest) one.
        row = brief.compute_daily_metrics(doc, now=NOW)
        self.assertEqual(row["decisions_pending_at_brief"], 1)

    def test_accepted_unedited_is_a_ratio(self):
        # 1 accepted-with-default + 1 rejected + 1 edited resolved in 24h →
        # ratio 1/3, which a raw count (1) can never equal.
        a, _ = decisions.open_decision(event=make_event(1), lane="staged",
                                       policy_id="p", now=NOW - 3600)
        b, _ = decisions.open_decision(event=make_event(2), lane="staged",
                                       policy_id="p", now=NOW - 3600)
        c, _ = decisions.open_decision(event=make_event(3), lane="staged",
                                       policy_id="p", now=NOW - 3600)
        decisions.transition(a["id"], "accepted", via="test", now=NOW - 60)
        decisions.transition(b["id"], "rejected", via="test", now=NOW - 60)
        decisions.transition(c["id"], "edited", via="test", now=NOW - 60)
        doc = brief.build_brief(now=NOW)
        row = brief.compute_daily_metrics(doc, now=NOW)
        self.assertAlmostEqual(row["decisions_accepted_unedited"], 1 / 3,
                               places=3)

    def test_accepted_unedited_day_one_is_zero_not_div_error(self):
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", now=NOW - 3600)
        doc = brief.build_brief(now=NOW)
        row = brief.compute_daily_metrics(doc, now=NOW)  # no resolutions yet
        self.assertEqual(row["decisions_accepted_unedited"], 0.0)


class ReceiptsMergeDispatchedTests(BriefBase):
    """F16: an overnight PR merge (ledger kind "merge-dispatched", the kind
    pulse.py actually writes) shows up in the Handled-overnight receipts."""

    def test_merge_dispatched_row_appears_in_receipts(self):
        decisions.ledger_path().parent.mkdir(parents=True, exist_ok=True)
        with open(decisions.ledger_path(), "a") as f:
            f.write(json.dumps({
                "ts": brief.utc_iso(NOW - 1800), "epoch": int(NOW - 1800),
                "key": "workspace:7-merge", "kind": "merge-dispatched",
                "ws_ref": "workspace:7", "outcome": "submitted",
                "evidence": "merge-pr-dispatch pr=#42 outcome=submitted"}) + "\n")
        doc = brief.build_brief(now=NOW)
        kinds = [r["kind"] for r in doc["handled_overnight"]]
        self.assertIn("merge-dispatched", kinds)


class LedgerWindowNoClipTests(BriefBase):
    """F11: the 24h windowed ledger reads scan to the window boundary — a huge
    incident day is counted in full, never clipped to a fixed line tail."""

    def test_five_thousand_in_window_denials_all_counted(self):
        n = 5000  # > LEDGER_TAIL_LINES (4000)
        decisions.ledger_path().parent.mkdir(parents=True, exist_ok=True)
        with open(decisions.ledger_path(), "a") as f:
            for i in range(n):
                f.write(json.dumps({
                    "ts": brief.utc_iso(NOW - 600), "epoch": int(NOW - 600),
                    "key": f"interrupt:denied:notify:k{i}",
                    "kind": "interrupt-denied", "ws_ref": "(interrupt-gate)",
                    "outcome": "skipped", "evidence": "denied"}) + "\n")
        health = brief._build_health(decisions.read_log(), NOW)
        self.assertEqual(health["interrupts"]["denied_24h"], n)


class ProvidersHealthTests(BriefBase):
    """A5: a dark provider (opted-in droid whose binary/settings are broken)
    books status=failed cost rows every pulse — the brief must surface it as
    FAILING rather than render green."""

    def _write_cost_rows(self, rows):
        bin_dir = str(brief._REPO / "bin")
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        import metering  # noqa: PLC0415
        p = metering.cost_ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_dark_droid_flags_failing_while_claude_healthy(self):
        ts = brief.utc_iso(int(NOW - 60))
        self._write_cost_rows([
            {"ts": ts, "caller": "triage", "provider": "droid", "status": "failed"},
            {"ts": ts, "caller": "strategist", "provider": "droid", "status": "failed"},
            {"ts": ts, "caller": "narrator", "provider": "droid", "status": "failed"},
            {"ts": ts, "caller": "lessons", "provider": "claude", "status": "ok"},
        ])
        health = brief._providers_health(NOW)
        self.assertTrue(health["droid"]["failing"])
        self.assertEqual(health["droid"]["failed"], 3)
        self.assertFalse(health["claude"]["failing"])
        # and it is surfaced in the full health block
        full = brief._build_health([], NOW)
        self.assertTrue(full["providers"]["droid"]["failing"])

    def test_healthy_droid_not_flagged(self):
        ts = brief.utc_iso(int(NOW - 60))
        self._write_cost_rows([
            {"ts": ts, "caller": "triage", "provider": "droid", "status": "ok"},
            {"ts": ts, "caller": "triage", "provider": "droid", "status": "failed"},
            {"ts": ts, "caller": "triage", "provider": "droid", "status": "ok"},
        ])
        health = brief._providers_health(NOW)
        self.assertFalse(health["droid"]["failing"])  # 1 fail, not trailing/all
        self.assertEqual(health["droid"]["failed"], 1)

    def test_no_ledger_is_empty_not_error(self):
        self.assertEqual(brief._providers_health(NOW), {})


class PerDecisionSeenTests(BriefBase):
    """F4: seen-ness is per DECISION — a decision viewed in ANY brief is never
    degraded, even when its own (older) brief's sidecar was never stamped."""

    def test_weekend_view_of_later_brief_protects_earlier_decision(self):
        # Saturday: a staged decision, and Saturday's brief lists it.
        sat = NOW
        rec, _ = decisions.open_decision(
            event=make_event(1, kind="stale_kind"), lane="staged",
            policy_id="rule-staged", ttl_h=None, now=sat - 3600)
        brief.write_brief(brief.build_brief(now=sat))
        # Monday: same decision still open, Monday's brief lists it too, and
        # the user VIEWS Monday's brief (only). Saturday's sidecar stays
        # unstamped (the renderer can only stamp the latest brief).
        mon = sat + 48 * 3600 + 7200  # Sat's brief is now > 48h old
        brief.write_brief(brief.build_brief(now=mon))
        ok, _ = brief.mark_seen(brief.local_date(mon), now=mon + 60)
        self.assertTrue(ok)
        # Degrade must NOT expire a decision the user actually reviewed.
        out = brief.degrade_unseen(now=mon + 120)
        self.assertNotIn(rec["id"], out["expired"])
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "open")


class MetricsConcurrencyTests(BriefBase):
    """F5: the once-per-date metrics append is flock-guarded — concurrent
    builders yield exactly one row per date."""

    def test_concurrent_append_yields_one_row(self):
        import threading
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", now=NOW - 3600)
        doc = brief.build_brief(now=NOW)
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            brief.append_daily_metrics(doc, now=NOW)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(brief.read_daily_metrics()), 1)


class WriteBriefUniqueTmpTests(BriefBase):
    """F5b: the brief tmp file is unique per writer, so two builders racing on
    the same date can't collide on a fixed tmp name (FileNotFoundError)."""

    def test_concurrent_write_brief_same_date_no_error(self):
        import threading
        doc = brief.build_brief(now=NOW)
        errors = []

        def worker():
            try:
                brief.write_brief(doc)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(brief.brief_path(brief.local_date(NOW)).exists())


class PulseDegradeOrderingTests(BriefBase):
    """F9/F10: degradation runs BEFORE the brief is built and on the daily
    boundary regardless of whether today's brief already exists."""

    def _seed_old_unseen_brief_with_expiring_decision(self):
        old = NOW - 60 * 3600  # > 48h before NOW
        rec, _ = decisions.open_decision(
            event=make_event(1, kind="stale_kind"), lane="staged",
            policy_id="rule-staged", ttl_h=None, now=old - 3600)
        brief.write_brief(brief.build_brief(now=old))  # unseen old brief
        return rec["id"]

    def test_brief_never_lists_a_row_it_is_about_to_expire(self):
        dec_id = self._seed_old_unseen_brief_with_expiring_decision()
        out = brief.pulse_step(now=NOW)
        self.assertTrue(out["built"])
        # Degrade ran first → the decision is expired → today's brief, built
        # from the post-degrade state, does NOT list it (no dead one-tap row).
        today = brief._read_json(brief.brief_path(brief.local_date(NOW)))
        self.assertNotIn(dec_id, [r["id"] for r in today["queue"]])
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[dec_id]["status"], "expired")

    def test_degrade_runs_even_when_brief_preexists(self):
        dec_id = self._seed_old_unseen_brief_with_expiring_decision()
        # A pre-wake CLI build already wrote today's brief …
        brief.write_brief(brief.build_brief(now=NOW))
        self.assertTrue(brief.brief_path(brief.local_date(NOW)).exists())
        # … the pulse must STILL run the day's degradation, not early-return.
        out = brief.pulse_step(now=NOW)
        self.assertFalse(out["built"])           # brief already existed
        self.assertIsNotNone(out["degrade"])     # degrade still ran
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[dec_id]["status"], "expired")

    def test_degrade_gated_once_per_day(self):
        self._seed_old_unseen_brief_with_expiring_decision()
        first = brief.pulse_step(now=NOW)
        self.assertIsNotNone(first["degrade"])
        second = brief.pulse_step(now=NOW + 300)  # same day, later pulse
        self.assertIsNone(second["degrade"])      # stamp gates the re-run


class QueuePartitionTests(BriefBase):
    """F6: the escalate lane is always at the top of the queue, even when a
    lower lane's urgency+freshness would out-score it numerically."""

    def test_escalate_low_outranks_staged_now(self):
        decisions.open_decision(event=make_event(1), lane="staged",
                                policy_id="p", urgency="now", now=NOW - 60)
        decisions.open_decision(event=make_event(2), lane="escalate",
                                policy_id="p", urgency="low", now=NOW - 60)
        doc = brief.build_brief(now=NOW)
        self.assertEqual(doc["queue"][0]["lane"], "escalate")


if __name__ == "__main__":
    unittest.main()
