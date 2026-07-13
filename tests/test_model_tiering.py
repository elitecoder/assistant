"""Tests for Keel M8 model-tiering: the Haiku downgrades + the periodic FRONTIER
shadow-audit of the Observer.

Proves, with NO live LLM (call_observer_batch is mocked):

  • Haiku routing: triage/extractor/mem0 default to a Haiku id and are
    env-overridable; triage no longer rides the Observer's model;
  • the audit is RECORDS-ONLY and drives nothing (it never calls execute_verdict);
  • cadence gate: at most one audit per window (per-window stamp);
  • ceiling gate: over-ceiling → no spend (frontier sheds first); a broken gate
    fails CLOSED (no spend);
  • sampling prefers action-bearing (non-`active`) verdicts and caps at k;
  • drift accounting: agree/disagree computed per ws, a ws the frontier didn't
    judge is skipped (never a fabricated diff), rows land in the drift ledger;
  • the brief Health derives the agreement summary purely from that ledger.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
BIN = REPO / "bin"
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))


def _load_pulse():
    spec = importlib.util.spec_from_file_location("pulse_mod", str(BIN / "pulse.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("TRIAGE_MODEL", "EXTRACTOR_MODEL", "MEM0_LLM_MODEL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_downgraded_endpoints_default_to_haiku_and_off_the_observer(self):
        pulse = _load_pulse()
        self.assertIn("haiku", pulse.TRIAGE_MODEL)
        # triage must NOT ride the Observer's Sonnet
        self.assertNotEqual(pulse.TRIAGE_MODEL, pulse.DEFAULT_OBSERVER_MODEL)
        self.assertIn("sonnet", pulse.DEFAULT_OBSERVER_MODEL)  # Observer stays
        extractor = _load_module("lesson_extractor", BIN / "lesson-extractor.py")
        self.assertIn("haiku", extractor.EXTRACTOR_MODEL)

    def test_env_override_wins(self):
        os.environ["TRIAGE_MODEL"] = "us.anthropic.claude-sonnet-4-6[1m]"
        pulse = _load_pulse()
        self.assertEqual(pulse.TRIAGE_MODEL,
                         "us.anthropic.claude-sonnet-4-6[1m]")

    def test_audit_model_is_frontier(self):
        pulse = _load_pulse()
        self.assertIn("opus", pulse.OBSERVER_AUDIT_MODEL)

    def test_bad_audit_env_does_not_crash_import(self):
        # A typo in a numeric audit tunable must fall back to the default, NOT
        # take the whole pulse down at import (M8 review M1).
        saved = {k: os.environ.get(k) for k in
                 ("OBSERVER_AUDIT_SAMPLE", "OBSERVER_AUDIT_INTERVAL_HOURS")}
        os.environ["OBSERVER_AUDIT_SAMPLE"] = "all"
        os.environ["OBSERVER_AUDIT_INTERVAL_HOURS"] = "2h"
        try:
            pulse = _load_pulse()  # must not raise
            self.assertEqual(pulse.OBSERVER_AUDIT_SAMPLE, 6)
            self.assertEqual(pulse.OBSERVER_AUDIT_INTERVAL_HOURS, 2.0)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class _AuditBase(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        self._old = {k: os.environ.get(k) for k in
                     ("HOME", "OBSERVER_AUDIT", "OBSERVER_AUDIT_SAMPLE")}
        os.environ["HOME"] = str(self._tmp)
        os.environ["OBSERVER_AUDIT"] = "1"
        os.environ["OBSERVER_AUDIT_SAMPLE"] = "6"
        self.now = time.time()
        self.pulse = _load_pulse()
        # gate open by default; individual tests may flip it
        from assistant import strategist  # noqa: PLC0415
        self._strat = strategist
        self._real_ceiling = strategist.over_ceiling
        strategist.over_ceiling = lambda now: False
        # a fake Observer batch: frontier verdicts injected per test
        self._frontier = {}
        self._spawn_calls = []

        def fake_batch(ctxs, pulse_idx, batch_idx, *, model=None, label="batch"):
            self._spawn_calls.append({"model": model, "label": label,
                                      "n": len(ctxs)})
            return ({c["ws_ref"]: {"verdict": self._frontier[c["ws_ref"]]}
                     for c in ctxs if c["ws_ref"] in self._frontier}, {
                         "tokens_in": 10,
                         "tokens_out": 1,
                         "cost_usd": 0.01,
                         "source": "cli",
                         "provider": "droid",
                         "model": "glm-5.2",
                     })
        self.pulse.call_observer_batch = fake_batch

    def tearDown(self):
        self._strat.over_ceiling = self._real_ceiling
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp_obj.cleanup()

    def _ctxs(self, *refs):
        return [{"ws_ref": r, "transcript_path": f"/x/{r}.jsonl"} for r in refs]


class AuditGateTests(_AuditBase):
    def test_fires_once_per_window_then_stamps(self):
        ctxs = self._ctxs("ws:1", "ws:2")
        sonnet = {"ws:1": {"verdict": "stranded"}, "ws:2": {"verdict": "active"}}
        self._frontier = {"ws:1": "stranded", "ws:2": "active"}
        out1 = self.pulse.run_observer_audit(ctxs, sonnet, 0, now=self.now)
        self.assertTrue(out1["ran"])
        self.assertEqual(len(self._spawn_calls), 1)
        # same window → no second spawn
        out2 = self.pulse.run_observer_audit(ctxs, sonnet, 1, now=self.now + 60)
        self.assertFalse(out2["ran"])
        self.assertEqual(out2["reason"], "already-audited-window")
        self.assertEqual(len(self._spawn_calls), 1)

    def test_next_window_fires_again(self):
        ctxs = self._ctxs("ws:1")
        sonnet = {"ws:1": {"verdict": "active"}}
        self._frontier = {"ws:1": "active"}
        self.pulse.run_observer_audit(ctxs, sonnet, 0, now=self.now)
        later = self.now + self.pulse.OBSERVER_AUDIT_INTERVAL_HOURS * 3600 + 1
        out = self.pulse.run_observer_audit(ctxs, sonnet, 1, now=later)
        self.assertTrue(out["ran"])
        self.assertEqual(len(self._spawn_calls), 2)

    def test_over_ceiling_sheds_the_frontier_audit(self):
        self._strat.over_ceiling = lambda now: True
        out = self.pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "ceiling-shed")
        self.assertEqual(self._spawn_calls, [])  # never spent

    def test_disabled_flag_is_a_noop(self):
        os.environ["OBSERVER_AUDIT"] = "0"
        pulse = _load_pulse()
        pulse.call_observer_batch = self.pulse.call_observer_batch
        out = pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertFalse(out["ran"])

    def test_unreservable_stamp_fails_closed_no_spend(self):
        # If the window stamp can't be written durably (here: ~/.assistant/audit
        # exists as a FILE, so mkdir raises), the audit must SKIP, never spend —
        # else it'd re-fire every pulse into a frontier-spend loop (M8 review B2).
        (self._tmp / ".assistant" / "audit").write_text("i am a file not a dir")
        self._frontier = {"ws:1": "active"}
        out = self.pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "stamp-unreservable")
        self.assertEqual(self._spawn_calls, [])  # never spent

    def test_uses_the_frontier_model_and_audit_label(self):
        self._frontier = {"ws:1": "active"}
        self.pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertEqual(self._spawn_calls[0]["model"],
                         self.pulse.OBSERVER_AUDIT_MODEL)
        self.assertEqual(self._spawn_calls[0]["label"], "audit")
        ledger = self._tmp / ".assistant/cost-ledger.jsonl"
        row = json.loads(ledger.read_text().splitlines()[0])
        self.assertEqual(row["provider"], "droid")
        self.assertEqual(row["model"], "glm-5.2")


class AuditSampleTests(_AuditBase):
    def test_prefers_action_bearing_verdicts_and_caps(self):
        ctxs = self._ctxs("ws:1", "ws:2", "ws:3", "ws:4")
        sonnet = {"ws:1": {"verdict": "active"}, "ws:2": {"verdict": "stranded"},
                  "ws:3": {"verdict": "active"},
                  "ws:4": {"verdict": "ready_for_merge"}}
        sample = self.pulse._audit_sample(ctxs, sonnet, 3)
        refs = [c["ws_ref"] for c in sample]
        self.assertEqual(len(refs), 3)
        # the two consequential verdicts come first
        self.assertEqual(set(refs[:2]), {"ws:2", "ws:4"})


class AuditDriftTests(_AuditBase):
    def test_agreement_and_disagreement_are_ledgered(self):
        ctxs = self._ctxs("ws:1", "ws:2", "ws:3")
        sonnet = {"ws:1": {"verdict": "stranded"},
                  "ws:2": {"verdict": "active"},
                  "ws:3": {"verdict": "ready_for_cleanup"}}
        # frontier agrees on ws:1, disagrees on ws:2, and DIDN'T judge ws:3
        self._frontier = {"ws:1": "stranded", "ws:2": "stranded"}
        out = self.pulse.run_observer_audit(ctxs, sonnet, 0, now=self.now)
        self.assertTrue(out["ran"])
        self.assertEqual(out["compared"], 2)      # ws:3 skipped (no frontier read)
        self.assertEqual(out["agreed"], 1)        # ws:1
        self.assertEqual(out["disagreements"], 1)  # ws:2
        self.assertEqual(out["diff_ws"], ["ws:2"])
        # ledger rows
        led = self._tmp / ".assistant" / "audit" / "observer-drift.jsonl"
        rows = [json.loads(x) for x in led.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        by = {r["ws_ref"]: r for r in rows}
        self.assertTrue(by["ws:1"]["agreed"])
        self.assertFalse(by["ws:2"]["agreed"])
        self.assertEqual(by["ws:2"]["sonnet"], "active")
        self.assertEqual(by["ws:2"]["frontier"], "stranded")
        self.assertEqual(by["ws:2"]["provider_audit"], "droid")
        self.assertEqual(by["ws:2"]["model_audit"], "glm-5.2")

    def test_driver_route_is_ledgered_per_workspace(self):
        self._frontier = {"ws:1": "active"}
        self.pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0,
            now=self.now,
            driver_routes_by_ws={
                "ws:1": {"provider": "droid", "model": "glm-5.2"},
            },
        )
        ledger = (
            self._tmp / ".assistant/audit/observer-drift.jsonl"
        )
        row = json.loads(ledger.read_text().splitlines()[0])
        self.assertEqual(row["provider_driver"], "droid")
        self.assertEqual(row["model_driver"], "glm-5.2")

    def test_records_only_never_executes(self):
        """The audit path must not call execute_verdict — it drives nothing."""
        called = []
        self.pulse.execute_verdict = lambda *a, **k: called.append(1)
        self._frontier = {"ws:1": "stranded"}
        self.pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertEqual(called, [])

    def test_failing_frontier_writes_ok_false_status_row(self):
        # Frontier judged nothing (rejected spawn) → zero drift rows, but a
        # status row with ok=False so the brief can see the audit is FAILING
        # rather than read the empty drift ledger as agreement (M8 review M2).
        self._frontier = {}  # frontier returns no verdicts for any ws
        out = self.pulse.run_observer_audit(
            self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertTrue(out["ran"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["compared"], 0)
        drift = self._tmp / ".assistant" / "audit" / "observer-drift.jsonl"
        self.assertFalse(drift.exists())  # no fabricated comparisons
        status = self._tmp / ".assistant" / "audit" / "observer-audit-status.jsonl"
        rows = [json.loads(x) for x in status.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["provider_audit"], "droid")
        self.assertEqual(rows[0]["model_audit"], "glm-5.2")


class BriefHealthDerivationTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant" / "audit").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self._tmp)
        from assistant import brief  # noqa: PLC0415
        self.brief = brief
        self.now = time.time()

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def test_health_summarises_the_drift_ledger(self):
        led = self._tmp / ".assistant" / "audit" / "observer-drift.jsonl"
        rows = [
            {"ts": self.brief.utc_iso(self.now - 100), "epoch": int(self.now - 100),
             "ws_ref": "ws:1", "sonnet": "active", "frontier": "active",
             "agreed": True, "model_audit": "opus"},
            {"ts": self.brief.utc_iso(self.now - 50), "epoch": int(self.now - 50),
             "ws_ref": "ws:2", "sonnet": "active", "frontier": "stranded",
             "agreed": False, "model_audit": "opus"},
        ]
        led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        h = self.brief._observer_audit_health(self.now)
        self.assertTrue(h["available"])
        self.assertEqual(h["compared"], 2)
        self.assertEqual(h["agreed"], 1)
        self.assertEqual(h["disagreements"], 1)
        self.assertEqual(h["agree_rate"], 0.5)
        self.assertEqual(h["recent_diffs"][0]["ws_ref"], "ws:2")

    def test_absent_ledger_is_unavailable(self):
        self.assertFalse(self.brief._observer_audit_health(self.now)["available"])

    def test_failing_audit_is_surfaced_not_read_as_agreement(self):
        # Status rows show attempts, but the drift ledger is empty → the audit is
        # FAILING, not silently agreeing (M8 review M2). available=True so the
        # dashboard shows a loud FAILING chip instead of omitting the card.
        status = self._tmp / ".assistant" / "audit" / "observer-audit-status.jsonl"
        status.write_text(json.dumps({
            "ts": self.brief.utc_iso(self.now - 60), "epoch": int(self.now - 60),
            "ok": False, "compared": 0, "agreed": 0, "disagreements": 0,
            "model_audit": "opus", "reason": "frontier produced no verdicts"}) + "\n")
        h = self.brief._observer_audit_health(self.now)
        self.assertTrue(h["available"])
        self.assertTrue(h["failing"])
        self.assertEqual(h["compared"], 0)
        self.assertEqual(h["failed_attempts"], 1)
        self.assertEqual(h["last_error"], "frontier produced no verdicts")


class AuditProviderGuardTests(unittest.TestCase):
    """The frontier shadow-audit must stay a DIFFERENT judge from the driver.
    Even when config routes the observer to droid, the audit is pinned to
    claude + the frontier model so it can't collapse into droid/glm-5.2 auditing
    itself (self-agreement ~100% by construction) while rendering a green
    "frontier audit" chip. Exercises the REAL call_observer_batch routing (the
    _AuditBase tests mock it out, so they never covered this)."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant" / "comms").mkdir(parents=True)
        # Route the WHOLE fleet (both observer sections) to droid.
        (self._tmp / ".assistant" / "comms" / "config.json").write_text(
            json.dumps({"llm": {"provider": "droid",
                                "droid": {"bin": "/x/droid",
                                          "model": "glm-5.2"}}}))
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self._tmp)
        self.pulse = _load_pulse()
        if str(BIN) not in os.sys.path:
            os.sys.path.insert(0, str(BIN))
        import llm_runner  # noqa: PLC0415
        self.lr = llm_runner
        self._real_invoke = llm_runner.invoke
        self.captured = []

        def fake_invoke(*, provider, model, **kw):
            self.captured.append({"provider": provider, "model": model})
            return llm_runner.LLMResult(
                provider=provider, model=model, rc=1, stdout="", stderr="",
                wall_ms=0, tokens_in=0, tokens_out=0, cost_usd=None,
                usage_source="none", session_id=None, result_text="",
                usable=False)
        llm_runner.invoke = fake_invoke

    def tearDown(self):
        self.lr.invoke = self._real_invoke
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _ctxs(self, *refs):
        return [{"ws_ref": r, "transcript_path": None, "cwd": None}
                for r in refs]

    def test_audit_pinned_to_claude_even_when_observer_routed_to_droid(self):
        self.pulse.call_observer_batch(
            self._ctxs("ws:1"), 0, 0,
            model=self.pulse.OBSERVER_AUDIT_MODEL, label="audit")
        self.assertEqual(self.captured[-1]["provider"], "claude")
        self.assertEqual(self.captured[-1]["model"],
                         self.pulse.OBSERVER_AUDIT_MODEL)

    def test_driver_batch_still_honors_droid_routing(self):
        # The guard is audit-specific: the driver batch still follows the
        # configured provider, so opting the fleet into droid is respected.
        self.pulse.call_observer_batch(
            self._ctxs("ws:1"), 0, 0,
            model=self.pulse.DEFAULT_OBSERVER_MODEL, label="batch")
        self.assertEqual(self.captured[-1]["provider"], "droid")
        self.assertEqual(self.captured[-1]["model"], "glm-5.2")


if __name__ == "__main__":
    unittest.main()
