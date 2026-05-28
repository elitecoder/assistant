"""Unit tests for bin/pulse.py — exercise the mechanical bits.

We DON'T spawn claude here. The Observer call is mocked via a sandboxed
HOME and a dummy claude binary that emits a verdict JSON. Everything
else (inbox drain, verdict→action lookup, NO_INGEST_GUARD, awaiting card
emission, back-off respect) is straight Python and runs in <2s.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PULSE_PATH = REPO / "bin/pulse.py"


def load_pulse(home: Path):
    """Import bin/pulse.py with HOME pointed at a tempdir."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("pulse_mod", str(PULSE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/inbox").mkdir(parents=True)
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    return tmp


class DrainInboxTests(unittest.TestCase):
    def test_drains_pulse_files_only(self):
        with TemporaryDirectory() as tmp:
            tmp = fixture_home(Path(tmp))
            inbox = tmp / ".assistant/inbox"
            (inbox / "pulse-1.json").write_text("{}")
            (inbox / "pulse-2.json").write_text("{}")
            (inbox / "other.txt").write_text("keep")
            mod = load_pulse(tmp)
            self.assertEqual(mod.drain_inbox(), 2)
            self.assertFalse((inbox / "pulse-1.json").exists())
            self.assertFalse((inbox / "pulse-2.json").exists())
            self.assertTrue((inbox / "other.txt").exists())

    def test_missing_inbox_dir_returns_zero(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)  # no .assistant subdir
            mod = load_pulse(tmp)
            self.assertEqual(mod.drain_inbox(), 0)


class ReadVerdictsFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _write(self, body: str) -> Path:
        p = self._tmp / "verdicts.jsonl"
        p.write_text(body)
        return p

    def test_parses_jsonl_keyed_by_ws_ref(self):
        body = textwrap.dedent("""\
            {"ws_ref": "workspace:1", "verdict": "active", "summary": "s1", "next": "n1"}
            {"ws_ref": "workspace:2", "verdict": "needs_user", "summary": "s2", "next": "n2", "title": "t", "detail": "d"}
            """)
        v = self.mod.read_verdicts_file(self._write(body))
        self.assertEqual(set(v.keys()), {"workspace:1", "workspace:2"})
        self.assertEqual(v["workspace:1"]["verdict"], "active")
        self.assertEqual(v["workspace:2"]["title"], "t")

    def test_skips_lines_without_ws_ref_or_verdict(self):
        body = textwrap.dedent("""\
            {"ws_ref": "workspace:1", "verdict": "active", "summary": "s", "next": "n"}
            {"verdict": "active", "summary": "no ref"}
            {"ws_ref": "workspace:2"}
            random text
            ```json
            {"ws_ref": "workspace:3", "verdict": "no_action", "summary": "s", "next": "n"}
            ```
            """)
        v = self.mod.read_verdicts_file(self._write(body))
        self.assertIn("workspace:1", v)
        self.assertIn("workspace:3", v)
        self.assertNotIn("workspace:2", v)

    def test_missing_file_returns_empty(self):
        from pathlib import Path as P
        self.assertEqual(self.mod.read_verdicts_file(P("/nonexistent/path.jsonl")), {})

    def test_empty_file_returns_empty(self):
        self.assertEqual(self.mod.read_verdicts_file(self._write("")), {})


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_chunks_evenly(self):
        self.assertEqual(self.mod.chunk(list(range(20)), 10),
                         [list(range(10)), list(range(10, 20))])

    def test_partial_last_chunk(self):
        self.assertEqual(self.mod.chunk(list(range(7)), 3),
                         [[0,1,2],[3,4,5],[6]])

    def test_empty(self):
        self.assertEqual(self.mod.chunk([], 10), [])

    def test_smaller_than_size(self):
        self.assertEqual(self.mod.chunk([1,2], 10), [[1, 2]])


class ExecuteVerdictTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _ws(self, ref="workspace:99"):
        return {"ref": ref, "title": "t", "cwd": "/tmp"}

    def test_active_is_noop(self):
        action = self.mod.execute_verdict(
            self._ws(), {"verdict": "active", "summary": "s", "next": "n"}, [],
        )
        self.assertEqual(action["kind"], "noop")

    def test_no_action_is_noop(self):
        action = self.mod.execute_verdict(
            self._ws(), {"verdict": "no_action", "summary": "s", "next": "n"}, [],
        )
        self.assertEqual(action["kind"], "noop")

    def test_needs_user_emits_card(self):
        awaiting = []
        self.mod.execute_verdict(
            self._ws(), {
                "verdict": "needs_user",
                "summary": "s", "next": "n",
                "title": "PR ready", "detail": "approve please",
            },
            awaiting,
        )
        self.assertEqual(len(awaiting), 1)
        self.assertEqual(awaiting[0]["title"], "PR ready")
        self.assertIn("approve please", awaiting[0]["detail"])
        self.assertEqual(awaiting[0]["ws_ref"], "workspace:99")

    def test_unknown_verdict_does_not_send(self):
        # Patch cmux_send to detect any call.
        called = {"n": 0}
        with mock.patch.object(self.mod, "cmux_send",
                               lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {}):
            self.mod.execute_verdict(
                self._ws(),
                {"verdict": "wat", "summary": "s", "next": "n"},
                [],
            )
        self.assertEqual(called["n"], 0)

    def test_stranded_without_nudge_skips(self):
        with mock.patch.object(self.mod, "cmux_send", lambda *a, **k: {}):
            action = self.mod.execute_verdict(
                self._ws(),
                {"verdict": "stranded", "summary": "s", "next": "n"},  # no nudge_text
                [],
            )
        self.assertEqual(action["kind"], "skipped")
        self.assertEqual(action["outcome"], "failed")

    def test_ready_for_cleanup_calls_cmux_send_with_cleanup(self):
        seen = {}
        def fake_send(ws_ref, text, **k):
            seen["ws_ref"] = ws_ref
            seen["text"] = text
            return {"outcome": "sent", "transcript_size_delta": 1234}
        with mock.patch.object(self.mod, "cmux_send", fake_send):
            with mock.patch.object(self.mod, "previous_send_ingested", lambda *a: True):
                action = self.mod.execute_verdict(
                    self._ws(), {"verdict": "ready_for_cleanup", "summary": "s", "next": "n"}, [],
                )
        self.assertEqual(seen, {"ws_ref": "workspace:99", "text": "/cleanup"})
        self.assertEqual(action["outcome"], "verified")

    def test_ready_for_merge_calls_cmux_send_with_merge_when_ready(self):
        seen = {}
        def fake_send(ws_ref, text, **k):
            seen["text"] = text
            return {"outcome": "sent", "transcript_size_delta": 500}
        with mock.patch.object(self.mod, "cmux_send", fake_send):
            with mock.patch.object(self.mod, "previous_send_ingested", lambda *a: True):
                self.mod.execute_verdict(
                    self._ws(), {"verdict": "ready_for_merge", "summary": "s", "next": "n"}, [],
                )
        self.assertEqual(seen["text"], "/merge-when-ready")


class NoIngestGuardTests(unittest.TestCase):
    """The bug that drove this rewrite: cleanup loop, 22 sends, all delta=0.
    NO_INGEST_GUARD breaks the loop after the first failure."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _write_send_log(self, records: list[dict]):
        path = self._tmp / ".assistant/sends.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_skip_when_prior_send_was_no_ingest(self):
        self._write_send_log([
            {"target_ws_ref": "workspace:7", "text": "/cleanup",
             "transcript_size_delta": 0, "outcome": "sent"},
        ])
        self.assertFalse(self.mod.previous_send_ingested("workspace:7", "/cleanup"))

    def test_send_when_prior_send_ingested(self):
        self._write_send_log([
            {"target_ws_ref": "workspace:7", "text": "/cleanup",
             "transcript_size_delta": 1024, "outcome": "sent"},
        ])
        self.assertTrue(self.mod.previous_send_ingested("workspace:7", "/cleanup"))

    def test_send_when_prior_send_was_different_text(self):
        # If last send was a /merge-when-ready, a new /cleanup is OK.
        self._write_send_log([
            {"target_ws_ref": "workspace:7", "text": "/merge-when-ready",
             "transcript_size_delta": 0, "outcome": "sent"},
        ])
        self.assertTrue(self.mod.previous_send_ingested("workspace:7", "/cleanup"))

    def test_send_when_no_history(self):
        # No sends.jsonl at all → assume ok.
        self.assertTrue(self.mod.previous_send_ingested("workspace:7", "/cleanup"))

    def test_execute_verdict_skips_when_guard_fires(self):
        self._write_send_log([
            {"target_ws_ref": "workspace:7", "text": "/cleanup",
             "transcript_size_delta": 0, "outcome": "sent"},
        ])
        sent = []
        with mock.patch.object(self.mod, "cmux_send",
                               lambda *a, **k: sent.append(a) or {"outcome": "sent"}):
            action = self.mod.execute_verdict(
                {"ref": "workspace:7", "title": "t", "cwd": "/"},
                {"verdict": "ready_for_cleanup", "summary": "s", "next": "n"},
                [],
            )
        self.assertEqual(sent, [], "cmux_send must NOT be called when guard fires")
        self.assertEqual(action["kind"], "skipped")


class CountActiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_counts_working_status(self):
        meta = [
            {"ctx": {"agent_status": "working", "last_turn_age_sec": 9999}},
            {"ctx": {"agent_status": "idle", "last_turn_age_sec": 100}},  # young → active
            {"ctx": {"agent_status": "idle", "last_turn_age_sec": 9999}},  # old → not active
        ]
        self.assertEqual(self.mod.count_active(meta), 2)

    def test_unknown_age_not_counted(self):
        meta = [{"ctx": {"agent_status": "idle", "last_turn_age_sec": None}}]
        self.assertEqual(self.mod.count_active(meta), 0)


if __name__ == "__main__":
    unittest.main()
