"""The pulse counts the failures it used to repeat silently."""
import json
import subprocess
import sys

from assistant import failwatch
from test_model_tiering import _AuditBase
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_pulse import fixture_home, load_pulse


class AuditPauseTests(_AuditBase):
    def _repeat(self):
        for _ in range(3):
            failwatch.record_failure("model:observer-audit", "droid glm call failed: exit code 1")

    def test_a_paused_audit_makes_no_call_and_keeps_its_window(self):
        self._repeat()
        out = self.pulse.run_observer_audit(self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        self.assertEqual((out["ran"], out["reason"]), (False, "model-paused"))
        self.assertEqual(self._spawn_calls, [])
        self.assertFalse(self.pulse.audit_window_stamp(int(self.now // 7200)).exists())

    def test_an_audit_that_returns_nothing_is_counted_and_one_that_works_clears_it(self):
        self._frontier = {}
        self.pulse.run_observer_audit(self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        (row,) = failwatch.repeated(min_count=1)
        self.assertEqual(row["component"], "model:observer-audit")
        self.assertEqual(row["sample"], "droid glm-5.2 call failed: the model returned no verdicts")
        self._frontier = {"ws:1": "active"}
        later = self.now + 2 * 7200
        self.pulse.run_observer_audit(self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 1, now=later)
        self.assertEqual(failwatch.history()[0]["streak"], 0)

    def test_the_pause_check_finds_metering_when_bin_is_not_on_the_path(self):
        self._repeat()
        saved = sys.path[:]
        sys.path[:] = [p for p in sys.path if p != str(self.pulse.BIN)]
        try:
            out = self.pulse.run_observer_audit(self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        finally:
            sys.path[:] = saved
        self.assertEqual(out["reason"], "model-paused")

    def test_a_broken_pause_check_never_blocks_the_audit(self):
        import metering
        self._repeat()
        real = metering.paused_note
        metering.paused_note = lambda caller: (_ for _ in ()).throw(RuntimeError("bug"))
        try:
            self._frontier = {"ws:1": "active"}
            out = self.pulse.run_observer_audit(self._ctxs("ws:1"), {"ws:1": {"verdict": "active"}}, 0, now=self.now)
        finally:
            metering.paused_note = real
        self.assertTrue(out["ran"])


class ReadyForMergeCountTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.mod = load_pulse(fixture_home(Path(self._tmp_obj.name)))

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _ws(self, ref="workspace:99"):
        return {"ref": ref, "title": "t", "cwd": "/tmp"}

    def test_ready_for_merge_with_no_pr_is_counted_once_per_workspace_and_the_ledger_row_is_unchanged(self):
        verdict = {"verdict": "ready_for_merge", "summary": "s", "next": "n"}
        for _ in range(3):
            action = self.mod.execute_verdict(self._ws(), verdict, [])
        self.mod.execute_verdict(self._ws("workspace:5"), verdict, [])
        self.assertEqual((action["kind"], action["outcome"], action["evidence"]),
                         ("skipped", "failed", "ready_for_merge but no pr_refs on ws"))
        rows = {r["component"]: r for r in failwatch.repeated(min_count=1)}
        self.assertEqual(set(rows), {"ready-for-merge:workspace:99", "ready-for-merge:workspace:5"})
        row = rows["ready-for-merge:workspace:99"]
        self.assertEqual((row["count"], row["backoff_until"]), (3, 0))
        self.assertEqual(row["sample"], "ready_for_merge but no pr_refs on ws")

    def test_a_counter_that_cannot_load_never_blocks_the_verdict(self):
        sys.modules["assistant.failwatch"] = None
        saved = sys.modules["assistant"].failwatch
        del sys.modules["assistant"].failwatch
        try:
            action = self.mod.execute_verdict(
                self._ws(), {"verdict": "ready_for_merge", "summary": "s", "next": "n"}, [])
        finally:
            sys.modules["assistant.failwatch"] = saved
            sys.modules["assistant"].failwatch = saved
        self.assertEqual(action["evidence"], "ready_for_merge but no pr_refs on ws")

    def test_the_merge_dispatch_runs_under_the_running_interpreter(self):
        seen = []
        real = subprocess.run
        subprocess.run = lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", "")
        try:
            self.mod.run_merge_pr_dispatch("workspace:1", 7, False, False)
        finally:
            subprocess.run = real
        self.assertEqual(seen[0][0], sys.executable)
