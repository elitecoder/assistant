"""Unit tests for bin/metering.py — the per-pulse cost/behavior metrics.

In-process, no LLM, no subprocess. Covers:
  - record shape (build_pulse_record emits exactly the metering contract)
  - verdict-change computation (prev-summary snapshot vs new verdicts)
  - usage capture (real CLI --output-format json envelope vs chars/4 estimate)
  - dashboard aggregation math (calls/day, $/day, change rate, skip rate)
  - metrics.jsonl rotation + robustness (corrupt line tolerated)
  - renderer tiles (render_metering_stats reads the log, degrades to '')
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
METERING_PATH = REPO / "bin/metering.py"
RENDERER_PATH = REPO / "bin/render-assistant-page.py"


def load_module(script: Path, name: str, home: Path):
    """Import a bin/ script with HOME pointed at a tempdir (repo test pattern)."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(name, str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    return tmp


class MeteringBase(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(METERING_PATH, "metering_mod", self.home)

    def tearDown(self):
        self._tmp_obj.cleanup()


# ─── record shape ────────────────────────────────────────────────────────────

class RecordShapeTests(MeteringBase):
    def _record(self, **overrides):
        kwargs = dict(
            epoch=1_700_000_000, pulse_idx=42, observer_called=True,
            batch_size=8, model="us.anthropic.claude-sonnet-4-6[1m]",
            duration_s=41.234,
            usage={"tokens_in": 91000, "tokens_out": 1800,
                   "cost_usd": 0.3, "source": "cli"},
            new_verdicts={"workspace:1": "active", "workspace:2": "active",
                          "workspace:3": "needs_user"},
            verdict_changes=1,
            actions=[{"kind": "noop"}, {"kind": "noop"}, {"kind": "emit-card"}],
        )
        kwargs.update(overrides)
        return self.mod.build_pulse_record(**kwargs)

    def test_record_has_exact_contract_keys(self):
        # "skipped" joined the contract in Keel M2 (Observer no-change skip):
        # workspaces whose prior verdict was carried forward without a call
        # this pulse (batch_size already excludes them).
        rec = self._record()
        self.assertEqual(set(rec.keys()), {
            "ts", "epoch", "pulse_idx", "observer_called", "batch_size",
            "model", "duration_s", "tokens_in", "tokens_out", "cost_usd_est",
            "usage_source", "verdicts", "verdict_changes", "synthesized",
            "skipped", "actions",
        })

    def test_record_values_and_counters(self):
        rec = self._record()
        self.assertEqual(rec["ts"], "2023-11-14T22:13:20Z")
        self.assertEqual(rec["pulse_idx"], 42)
        self.assertTrue(rec["observer_called"])
        self.assertEqual(rec["batch_size"], 8)
        self.assertEqual(rec["duration_s"], 41.23)
        self.assertEqual(rec["tokens_in"], 91000)
        self.assertEqual(rec["tokens_out"], 1800)
        self.assertEqual(rec["cost_usd_est"], 0.3)
        self.assertEqual(rec["usage_source"], "cli")
        self.assertEqual(rec["verdicts"], {"active": 2, "needs_user": 1})
        self.assertEqual(rec["verdict_changes"], 1)
        self.assertEqual(rec["actions"], {"noop": 2, "emit-card": 1})

    def test_record_is_json_serializable(self):
        rec = self._record()
        self.assertEqual(json.loads(json.dumps(rec)), rec)

    def test_skipped_pulse_record(self):
        rec = self._record(observer_called=False, batch_size=0, model=None,
                           duration_s=0.0, usage={}, new_verdicts={},
                           verdict_changes=0, actions=[])
        self.assertFalse(rec["observer_called"])
        self.assertIsNone(rec["model"])
        self.assertEqual(rec["tokens_in"], 0)
        self.assertEqual(rec["cost_usd_est"], 0.0)
        self.assertEqual(rec["verdicts"], {})
        self.assertEqual(rec["actions"], {})

    def test_synthesized_defaults_to_zero(self):
        self.assertEqual(self._record()["synthesized"], 0)

    def test_skipped_defaults_to_zero_and_records_count(self):
        self.assertEqual(self._record()["skipped"], 0)
        self.assertEqual(self._record(skipped=4)["skipped"], 4)

    def test_synthesized_field_recorded(self):
        rec = self._record(synthesized=3)
        self.assertEqual(rec["synthesized"], 3)
        self.assertEqual(json.loads(json.dumps(rec))["synthesized"], 3)

    def test_verdict_changes_none_serializes_as_null(self):
        # Degraded prev-verdict snapshot: comparison is null, NOT dropped —
        # cost/usage fields in the same record stay populated.
        rec = self._record(verdict_changes=None)
        self.assertIsNone(rec["verdict_changes"])
        self.assertEqual(rec["tokens_in"], 91000)
        self.assertEqual(rec["cost_usd_est"], 0.3)
        self.assertIn('"verdict_changes": null', json.dumps(rec))


# ─── verdict-change computation ──────────────────────────────────────────────

class VerdictChangeTests(MeteringBase):
    def _plant_summary(self, ws_ref: str, body):
        p = self.home / ".assistant/observer-summaries" / f"{ws_ref.replace(':', '_')}.json"
        p.write_text(body if isinstance(body, str) else json.dumps(body))

    def test_count_changes_only_for_ws_in_both_maps(self):
        prev = {"workspace:1": "active", "workspace:2": "needs_user"}
        new = {"workspace:1": "ready_for_merge",   # changed
               "workspace:2": "needs_user",        # unchanged
               "workspace:9": "active"}            # brand-new ws — not a change
        self.assertEqual(self.mod.count_verdict_changes(prev, new), 1)

    def test_count_changes_empty_maps(self):
        self.assertEqual(self.mod.count_verdict_changes({}, {}), 0)
        self.assertEqual(self.mod.count_verdict_changes({}, {"workspace:1": "active"}), 0)

    def test_load_prev_verdicts_reads_summary_files(self):
        self._plant_summary("workspace:1", {"ws_ref": "workspace:1", "verdict": "active"})
        self._plant_summary("workspace:2", {"ws_ref": "workspace:2", "verdict": "stranded"})
        prev = self.mod.load_prev_verdicts(["workspace:1", "workspace:2", "workspace:3"])
        self.assertEqual(prev, {"workspace:1": "active", "workspace:2": "stranded"})

    def test_load_prev_verdicts_skips_corrupt_and_verdictless(self):
        self._plant_summary("workspace:1", "{ corrupt")
        self._plant_summary("workspace:2", {"ws_ref": "workspace:2", "summary": "no verdict field"})
        prev = self.mod.load_prev_verdicts(["workspace:1", "workspace:2"])
        self.assertEqual(prev, {})

    def test_end_to_end_change_detection(self):
        self._plant_summary("workspace:5", {"verdict": "active"})
        prev = self.mod.load_prev_verdicts(["workspace:5"])
        self.assertEqual(
            self.mod.count_verdict_changes(prev, {"workspace:5": "ready_for_cleanup"}), 1)
        self.assertEqual(
            self.mod.count_verdict_changes(prev, {"workspace:5": "active"}), 0)


# ─── usage capture: CLI envelope vs estimate ─────────────────────────────────

class UsageCaptureTests(MeteringBase):
    ENVELOPE = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 41000, "num_turns": 12, "result": "done",
        "total_cost_usd": 0.3123,
        "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 40000,
                  "cache_read_input_tokens": 50000, "output_tokens": 1800},
    })

    def test_parse_cli_result_real_envelope(self):
        parsed = self.mod.parse_cli_result(self.ENVELOPE)
        self.assertEqual(parsed["tokens_in"], 91000)  # input + cache in/out
        self.assertEqual(parsed["tokens_out"], 1800)
        self.assertAlmostEqual(parsed["cost_usd"], 0.3123)

    def test_parse_cli_result_rejects_non_envelope(self):
        self.assertIsNone(self.mod.parse_cli_result("plain text transcript"))
        self.assertIsNone(self.mod.parse_cli_result(""))
        self.assertIsNone(self.mod.parse_cli_result('{"no": "usage"}'))

    def test_observer_usage_prefers_cli(self):
        u = self.mod.observer_usage(self.ENVELOPE, prompt_chars=999999,
                                    model="us.anthropic.claude-sonnet-4-6[1m]")
        self.assertEqual(u["source"], "cli")
        self.assertEqual(u["tokens_in"], 91000)
        self.assertAlmostEqual(u["cost_usd"], 0.3123)

    def test_observer_usage_falls_back_to_estimate(self):
        u = self.mod.observer_usage("not json at all", prompt_chars=4000,
                                    model="us.anthropic.claude-sonnet-4-6[1m]")
        self.assertEqual(u["source"], "estimated")
        self.assertEqual(u["tokens_in"], 1000)  # 4000 chars / 4
        # 1000 in @ $3/M + tokens_out @ $15/M — sonnet rates
        self.assertGreater(u["cost_usd"], 0)

    def test_sum_usage_source_mixing(self):
        cli = {"tokens_in": 10, "tokens_out": 1, "cost_usd": 0.1, "source": "cli"}
        est = {"tokens_in": 20, "tokens_out": 2, "cost_usd": 0.2, "source": "estimated"}
        self.assertEqual(self.mod.sum_usage([cli, cli])["source"], "cli")
        self.assertEqual(self.mod.sum_usage([est])["source"], "estimated")
        mixed = self.mod.sum_usage([cli, est])
        self.assertEqual(mixed["source"], "mixed")
        self.assertEqual(mixed["tokens_in"], 30)
        self.assertAlmostEqual(mixed["cost_usd"], 0.3)

    def test_sum_usage_empty(self):
        u = self.mod.sum_usage([])
        self.assertEqual(u["tokens_in"], 0)
        self.assertEqual(u["cost_usd"], 0.0)


# ─── dashboard aggregation math ──────────────────────────────────────────────

class AggregationTests(MeteringBase):
    NOW = 1_700_000_000

    def _rec(self, age_sec: int, *, called=True, batch=10, changes=0, cost=0.0):
        return {"ts": self.mod.utc_iso(self.NOW - age_sec),
                "epoch": self.NOW - age_sec, "pulse_idx": 1,
                "observer_called": called, "batch_size": batch,
                "cost_usd_est": cost, "verdict_changes": changes}

    def test_aggregate_math(self):
        records = [
            self._rec(86400, changes=2, cost=0.5),   # oldest → span = 1 day
            self._rec(43200, changes=1, cost=0.25),
            self._rec(0, called=False, batch=0, cost=0.0),  # deterministic skip
        ]
        agg = self.mod.aggregate(records, now=self.NOW, window_days=7)
        self.assertEqual(agg["n_pulses"], 3)
        self.assertAlmostEqual(agg["observer_calls_per_day"], 2.0)
        self.assertAlmostEqual(agg["cost_per_day_usd"], 0.75)
        self.assertAlmostEqual(agg["verdict_change_rate"], 3 / 20)  # changed/total judged
        self.assertAlmostEqual(agg["skip_rate"], 1 / 3)

    def test_aggregate_excludes_records_outside_window(self):
        records = [self._rec(8 * 86400, cost=99.0),  # 8 days old — outside 7d
                   self._rec(86400, cost=0.5)]
        agg = self.mod.aggregate(records, now=self.NOW, window_days=7)
        self.assertEqual(agg["n_pulses"], 1)
        self.assertAlmostEqual(agg["cost_per_day_usd"], 0.5)

    def test_aggregate_empty_returns_zeros(self):
        agg = self.mod.aggregate([], now=self.NOW)
        self.assertEqual(agg["n_pulses"], 0)
        self.assertEqual(agg["observer_calls_per_day"], 0.0)
        self.assertEqual(agg["cost_per_day_usd"], 0.0)
        self.assertEqual(agg["verdict_change_rate"], 0.0)
        self.assertEqual(agg["skip_rate"], 0.0)

    def test_aggregate_no_observer_calls_change_rate_zero(self):
        records = [self._rec(3600, called=False, batch=0)]
        agg = self.mod.aggregate(records, now=self.NOW)
        self.assertEqual(agg["verdict_change_rate"], 0.0)
        self.assertEqual(agg["skip_rate"], 1.0)

    def test_aggregate_includes_future_stamped_records(self):
        # Clock skew: a record stamped an hour in the future must NOT be
        # silently dropped — its cost is real and belongs in the total.
        records = [self._rec(86400, cost=0.5),        # oldest → span = 1 day
                   self._rec(-3600, cost=1.0)]        # 1h in the future
        agg = self.mod.aggregate(records, now=self.NOW, window_days=7)
        self.assertEqual(agg["n_pulses"], 2)
        self.assertAlmostEqual(agg["cost_per_day_usd"], 1.5)

    def test_aggregate_caps_absurdly_future_records(self):
        # Sanity cap: beyond now+1 day is garbage, not skew — still dropped.
        records = [self._rec(-2 * 86400, cost=99.0),  # 2 days in the future
                   self._rec(3600, cost=0.5)]
        agg = self.mod.aggregate(records, now=self.NOW, window_days=7)
        self.assertEqual(agg["n_pulses"], 1)

    def test_aggregate_tolerates_null_verdict_changes(self):
        # Degraded-comparison record (verdict_changes null) aggregates as 0
        # changes but its cost still counts.
        rec = self._rec(86400, cost=0.5)
        rec["verdict_changes"] = None
        agg = self.mod.aggregate([rec, self._rec(3600, changes=1, cost=0.5)],
                                 now=self.NOW, window_days=7)
        self.assertEqual(agg["n_pulses"], 2)
        self.assertAlmostEqual(agg["cost_per_day_usd"], 1.0)
        self.assertAlmostEqual(agg["verdict_change_rate"], 1 / 20)

    def test_aggregate_epoch_fallback_to_iso_ts(self):
        rec = self._rec(3600, cost=1.0)
        del rec["epoch"]  # older/foreign record — ts only
        agg = self.mod.aggregate([rec], now=self.NOW, window_days=7)
        self.assertEqual(agg["n_pulses"], 1)


# ─── metrics.jsonl append/rotation/robustness ───────────────────────────────

class MetricsFileTests(MeteringBase):
    def _path(self) -> Path:
        return self.home / ".assistant/metrics.jsonl"

    def test_append_and_read_roundtrip(self):
        rec = {"ts": "2026-07-09T00:00:00Z", "epoch": 1, "pulse_idx": 1}
        self.mod.append_metric(rec, self._path())
        self.mod.append_metric({**rec, "pulse_idx": 2}, self._path())
        got = self.mod.read_metrics(self._path())
        self.assertEqual([r["pulse_idx"] for r in got], [1, 2])

    def test_read_tolerates_corrupt_line(self):
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"pulse_idx": 1}\n{ torn line garbage\n\n{"pulse_idx": 2}\n')
        got = self.mod.read_metrics(p)
        self.assertEqual([r["pulse_idx"] for r in got], [1, 2])

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(self.mod.read_metrics(self._path()), [])

    def test_rotation_when_oversized(self):
        p = self._path()
        self.mod.append_metric({"pulse_idx": 1}, p, max_bytes=10)
        # File now exceeds 10 bytes → next append rotates it to .1 first.
        self.mod.append_metric({"pulse_idx": 2}, p, max_bytes=10)
        rotated = p.with_name(p.name + ".1")
        self.assertTrue(rotated.exists())
        self.assertEqual(
            [json.loads(l)["pulse_idx"] for l in rotated.read_text().splitlines()], [1])
        # read_metrics on the active path spans BOTH files, oldest-first, so
        # the dashboard window survives the rotation without truncating.
        self.assertEqual([r["pulse_idx"] for r in self.mod.read_metrics(p)], [1, 2])

    def test_read_spans_rotated_and_active_files(self):
        p = self._path()
        rotated = p.with_name(p.name + ".1")
        p.parent.mkdir(parents=True, exist_ok=True)
        rotated.write_text('{"pulse_idx": 1}\n{"pulse_idx": 2}\n')
        p.write_text('{"pulse_idx": 3}\n')
        self.assertEqual([r["pulse_idx"] for r in self.mod.read_metrics(p)],
                         [1, 2, 3])

    def test_read_tolerates_missing_or_corrupt_rotated_file(self):
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"pulse_idx": 5}\n')
        # No .1 file at all — active file alone still reads fine.
        self.assertEqual([r["pulse_idx"] for r in self.mod.read_metrics(p)], [5])
        # Corrupt .1 file — its bad lines are skipped, active file still read.
        p.with_name(p.name + ".1").write_text("{ total garbage\n")
        self.assertEqual([r["pulse_idx"] for r in self.mod.read_metrics(p)], [5])

    def test_no_rotation_under_limit(self):
        p = self._path()
        self.mod.append_metric({"pulse_idx": 1}, p)
        self.mod.append_metric({"pulse_idx": 2}, p)
        self.assertFalse(p.with_name(p.name + ".1").exists())
        self.assertEqual(len(self.mod.read_metrics(p)), 2)


# ─── renderer tiles ──────────────────────────────────────────────────────────

class RendererMeteringTilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = fixture_home(Path(self._tmp_obj.name))
        self.renderer = load_module(RENDERER_PATH, "renderer_metering_mod", self.home)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _plant_metrics(self):
        now = int(time.time())
        p = self.home / ".assistant/metrics.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"ts": "x", "epoch": now - 86400, "pulse_idx": 1, "observer_called": True,
             "batch_size": 10, "cost_usd_est": 0.5, "verdict_changes": 2},
            {"ts": "x", "epoch": now, "pulse_idx": 2, "observer_called": False,
             "batch_size": 0, "cost_usd_est": 0.0, "verdict_changes": 0},
        ]
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_tiles_render_from_metrics(self):
        self._plant_metrics()
        html = self.renderer.render_metering_stats()
        self.assertIn("Observer calls/day", html)
        self.assertIn("$/day est", html)
        self.assertIn("Verdict-change rate", html)
        self.assertIn("Skip rate", html)
        self.assertIn('class="stat"', html)

    def test_no_metrics_file_renders_nothing(self):
        self.assertEqual(self.renderer.render_metering_stats(), "")

    def test_corrupt_metrics_file_never_breaks_page(self):
        p = self.home / ".assistant/metrics.jsonl"
        p.write_text("{ total garbage\n")
        try:
            html = self.renderer.render_metering_stats()
        except Exception:
            self.fail("render_metering_stats must swallow corrupt metrics")
        self.assertEqual(html, "")

    def test_tiles_read_only_rotated_file_after_rotation(self):
        # Right after a rotation the recent records live in metrics.jsonl.1 —
        # the tiles must still render (path + spanning via metering module).
        now = int(time.time())
        p = self.home / ".assistant/metrics.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.with_name(p.name + ".1").write_text(json.dumps(
            {"ts": "x", "epoch": now - 3600, "pulse_idx": 1, "observer_called": True,
             "batch_size": 4, "cost_usd_est": 0.5, "verdict_changes": 1}) + "\n")
        p.write_text("")
        html = self.renderer.render_metering_stats()
        self.assertIn("Observer calls/day", html)

    def test_aggregate_missing_keys_degrades_to_empty(self):
        # Adversarial: aggregate() returning a dict WITHOUT the tile keys must
        # degrade the section to "" — a KeyError in the f-string previously
        # sat outside the try/except and killed the whole dashboard render.
        self._plant_metrics()
        sys.path.insert(0, str(REPO / "bin"))
        import metering as metering_mod  # same module object the renderer imports
        with mock.patch.object(metering_mod, "aggregate",
                               return_value={"n_pulses": 5}):
            try:
                html = self.renderer.render_metering_stats()
            except Exception:
                self.fail("render_metering_stats must swallow a partial aggregate")
        self.assertEqual(html, "")


if __name__ == "__main__":
    unittest.main()
