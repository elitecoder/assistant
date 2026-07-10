"""Tests for bin/pulse.py's triage-LLM caller (Keel M2): call_triage_batch
follows the Observer subprocess pattern — file-side-effect output
(lanes.jsonl), archived runs under ~/.assistant/triage-runs/, usage wrapped
in metering (cost-ledger row, caller="triage") — and read_lane_suggestions'
tolerant parse. The subprocess itself is simulated by stubbing pulse.run;
no LLM, no network.

Also pins the prompt file's suggestion-only posture: the lane vocabulary it
offers the model contains no `auto` and no `drop` (the code-side twin lives
in policy.TRIAGE_LANE_MAP's structural test).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PULSE_PATH = REPO / "bin/pulse.py"


def load_pulse(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("pulse_triage_llm_mod",
                                                  str(PULSE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_event(i=1, **over) -> dict:
    ev = {"schema": "world-event/1", "id": f"eid-{i}", "source": "github",
          "kind": "mystery", "external_id": f"x:{i}",
          "title": f"event {i}", "snippet": "?", "refs": {}}
    ev.update(over)
    return ev


class TriageCallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _fake_run(self, lanes_lines):
        """A stub for pulse.run that plays the subprocess: writes the given
        lines to <run_dir>/lanes.jsonl (found via the prompt text) and
        returns a usage-bearing CLI envelope on stdout."""
        def fake(cmd, *, input_text=None, timeout=30, env=None,
                 merge_bedrock=False):
            m = re.search(r"^\s+(/\S+/lanes\.jsonl)$", input_text or "",
                          re.MULTILINE)
            if m and lanes_lines is not None:
                Path(m.group(1)).write_text("\n".join(lanes_lines) + "\n")
            envelope = {"usage": {"input_tokens": 500, "output_tokens": 40},
                        "total_cost_usd": 0.002}
            return 0, json.dumps(envelope), ""
        return fake

    def test_empty_batch_is_a_noop(self):
        with mock.patch.object(self.mod, "run") as run_mock:
            self.assertEqual(self.mod.call_triage_batch([], 1), {})
        run_mock.assert_not_called()

    def test_archives_run_and_parses_suggestions(self):
        events = [make_event(1), make_event(2)]
        lines = [json.dumps({"event_id": "eid-1", "suggested_lane": "digest",
                             "rationale": "fyi"}),
                 "not json at all",
                 json.dumps({"event_id": "eid-2"}),  # no lane → dropped
                 json.dumps({"suggested_lane": "staged"})]  # no id → dropped
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run(lines)):
            out = self.mod.call_triage_batch(events, 7)
        self.assertEqual(out, {"eid-1": {"suggested_lane": "digest",
                                         "rationale": "fyi"}})
        run_dir = self._tmp / ".assistant/triage-runs/0007"
        for name in ("prompt.md", "events.json", "stdout.txt", "stderr.txt",
                     "meta.json", "lanes.jsonl"):
            self.assertTrue((run_dir / name).exists(), msg=name)
        meta = json.loads((run_dir / "meta.json").read_text())
        self.assertEqual(meta["n_events"], 2)
        self.assertEqual(meta["usage"]["source"], "cli")

    def test_cost_ledger_row_appended_for_caller_triage(self):
        with mock.patch.object(self.mod, "run",
                               side_effect=self._fake_run([])):
            self.mod.call_triage_batch([make_event(1)], 1)
        rows = [json.loads(l) for l in
                (self._tmp / ".assistant/cost-ledger.jsonl")
                .read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["caller"], "triage")
        self.assertEqual(rows[0]["tokens_in"], 500)
        self.assertEqual(rows[0]["est_usd"], 0.002)

    def test_subprocess_failure_returns_no_suggestions(self):
        with mock.patch.object(self.mod, "run",
                               return_value=(124, "", "timeout after 240s")):
            out = self.mod.call_triage_batch([make_event(1)], 1)
        self.assertEqual(out, {})  # fail-safe: decisions keep escalate

    def test_missing_prompt_file_skips_the_call(self):
        with mock.patch.object(self.mod, "TRIAGE_BATCH_PROMPT",
                               self._tmp / "nope.md"):
            with mock.patch.object(self.mod, "run") as run_mock:
                self.assertEqual(
                    self.mod.call_triage_batch([make_event(1)], 1), {})
        run_mock.assert_not_called()

    def test_prompt_offers_no_auto_and_no_drop_lane(self):
        # The prose twin of policy.TRIAGE_LANE_MAP's structural test: the
        # vocabulary offered to the model is exactly the three safe lanes.
        text = self.mod.TRIAGE_BATCH_PROMPT.read_text()
        self.assertIn("escalate|staged|digest", text)
        self.assertNotIn("| `auto` |", text)   # no auto row in the lane table
        self.assertNotIn("| `drop` |", text)   # no drop row either


class ReadLaneSuggestionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            self.mod.read_lane_suggestions(self._tmp / "gone.jsonl"), {})

    def test_fenced_and_torn_lines_skipped(self):
        p = self._tmp / "lanes.jsonl"
        p.write_text("```json\n"
                     '{"event_id": "e1", "suggested_lane": "staged"}\n'
                     "{torn\n```\n")
        out = self.mod.read_lane_suggestions(p)
        self.assertEqual(out["e1"]["suggested_lane"], "staged")
        self.assertEqual(out["e1"]["rationale"], "")


if __name__ == "__main__":
    unittest.main()
