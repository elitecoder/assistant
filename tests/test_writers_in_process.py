"""Direct-import tests for state-write, actions-ledger, transcript-tail,
assistant-curator. All four are CLI scripts with simple, testable
helpers and main() entrypoints."""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import io
import json
import os
import sys
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def load_module(home: Path, script: str):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(f"_{script}", str(REPO / "bin" / script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    (tmp / ".claude/projects").mkdir(parents=True)
    return tmp


# ─── state-write ────────────────────────────────────────────────────────────

class StateWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "state-write.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_utc_iso_format(self):
        s = self.mod.utc_iso()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_read_jsonl_tail_filters_by_epoch(self):
        path = Path(self._tmp / "log.jsonl")
        path.write_text("\n".join([
            json.dumps({"epoch": 100, "msg": "old"}),
            json.dumps({"epoch": 200, "msg": "new"}),
            json.dumps({"ts": "2026-05-28T00:00:00Z", "msg": "iso"}),
            "not json",
        ]))
        out = self.mod.read_jsonl_tail(str(path), since_epoch=150)
        msgs = [d.get("msg") for d in out]
        self.assertNotIn("old", msgs)
        self.assertIn("new", msgs)
        # ISO record gets epoch from ts parser → 2026-05-28 is way after 150.
        self.assertIn("iso", msgs)

    def test_read_jsonl_tail_returns_empty_when_path_missing(self):
        self.assertEqual(self.mod.read_jsonl_tail("/no/such/path", 0), [])

    def test_read_jsonl_tail_handles_malformed_iso(self):
        path = Path(self._tmp / "log.jsonl")
        path.write_text(json.dumps({"ts": "not a date", "msg": "x"}) + "\n")
        out = self.mod.read_jsonl_tail(str(path), 0)
        # Record kept since epoch is None → "no filter" branch.
        self.assertEqual(len(out), 1)

    def test_load_observer_summaries_parses_files(self):
        d = Path(self.mod.SUMMARIES_DIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / "workspace_1.json").write_text(json.dumps({
            "ws_ref": "workspace:1", "title": "title-a",
            "classification": "DONE", "summary": "ok"
        }))
        (d / "ignored.txt").write_text("not a summary")
        (d / "workspace_2.json").write_text("{ corrupt")
        # Workspace files start with "workspace_"
        (d / "workspace_3.json").write_text(json.dumps({
            "ws_ref": "workspace:3", "summary_for_next_pulse": "legacy summary",
        }))
        out = self.mod.load_observer_summaries()
        self.assertIn("workspace:1", out)
        self.assertIn("workspace:3", out)
        self.assertEqual(out["workspace:1"]["title"], "title-a")
        self.assertEqual(out["workspace:3"]["summary"], "legacy summary")

    def test_load_observer_summaries_missing_dir_returns_empty(self):
        self.mod.SUMMARIES_DIR = "/no/such/dir"
        self.assertEqual(self.mod.load_observer_summaries(), {})

    def test_write_trace_creates_markdown_file(self):
        state = {
            "_meta": {"pulse_idx": 7, "generated_at": "2026-05-28T12:00:00Z"},
            "actions_taken": [
                {"key": "k", "kind": "send", "outcome": "verified",
                 "evidence": "delta=42", "verified_via": "transcript_size_delta",
                 "target": {"ws": "workspace:1"}},
            ],
            "awaiting_input": [
                {"key": "ws-99", "tier": "T2", "title": "needs review"},
            ],
        }
        # Plant a sends.jsonl + ledger so all branches render.
        Path(self.mod.SENDS_LOG).write_text(json.dumps({
            "epoch": int(time.time()), "ts": "2026-05-28T12:00:00Z",
            "caller": "test", "target_ws_ref": "workspace:1",
            "target_ws_title": "x", "target_tty": "ttys000",
            "text": "/cleanup", "transcript_size_delta": 42, "outcome": "sent",
        }) + "\n" + json.dumps({
            # NO_INGEST path
            "epoch": int(time.time()), "ts": "2026-05-28T12:00:00Z",
            "target_ws_ref": "workspace:5", "text": "/cleanup",
            "transcript_size_delta": 0, "outcome": "sent",
        }) + "\n")
        Path(self.mod.LEDGER_PATH).write_text(json.dumps({
            "epoch": int(time.time()), "pulse_idx": 7, "key": "k1",
            "kind": "send", "outcome": "verified",
            "verified_via": "transcript_size_delta", "ws_ref": "workspace:1",
        }) + "\n" + json.dumps({
            # screen_read path → flagged
            "epoch": int(time.time()), "pulse_idx": 7, "key": "k2",
            "kind": "send", "outcome": "verified",
            "verified_via": "screen_read", "ws_ref": "workspace:1",
        }) + "\n" + json.dumps({
            # missing verified_via path → flagged
            "epoch": int(time.time()), "pulse_idx": 7, "key": "k3",
            "kind": "send", "outcome": "verified",
        }) + "\n")
        # Plant an observer summary so the Observers section renders.
        sd = Path(self.mod.SUMMARIES_DIR)
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "workspace_1.json").write_text(json.dumps({
            "ws_ref": "workspace:1", "classification": "DONE",
            "summary": "all good", "title": "test",
            "proposed_actions": [{"kind": "cleanup"}],
        }))

        self.mod.write_trace(state, pulse_started_epoch=int(time.time()) - 60)
        traces = list(Path(self.mod.TRACE_DIR).glob("pulse-*.md"))
        self.assertEqual(len(traces), 1)
        body = traces[0].read_text()
        self.assertIn("Pulse 7", body)
        self.assertIn("workspace:1", body)
        self.assertIn("**workspace:1**", body)
        self.assertIn("NO_INGEST", body)
        self.assertIn("SCREEN_READ", body)  # cross-check flagged screen_read
        self.assertIn("missing verified_via", body)

    def test_write_trace_handles_empty_state(self):
        # Every section should render its "(no ...)" placeholder.
        state = {"_meta": {"pulse_idx": 1, "generated_at": "2026-05-28T12:00:00Z"}}
        self.mod.write_trace(state, pulse_started_epoch=int(time.time()))
        traces = list(Path(self.mod.TRACE_DIR).glob("pulse-*.md"))
        body = traces[0].read_text()
        self.assertIn("(no observer summaries on disk)", body)
        self.assertIn("(no actions taken this pulse)", body)
        self.assertIn("(no sends this pulse)", body)
        self.assertIn("(no ledger writes this pulse)", body)
        self.assertIn("(no awaiting cards)", body)
        self.assertIn("(no anomalies)", body)

    def test_cleanup_old_traces_removes_old_files(self):
        d = Path(self.mod.TRACE_DIR)
        d.mkdir(parents=True, exist_ok=True)
        old = d / "pulse-old.md"
        old.write_text("x")
        os.utime(old, (time.time() - 100 * 3600, time.time() - 100 * 3600))
        new = d / "pulse-new.md"
        new.write_text("y")
        self.mod.cleanup_old_traces()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_cleanup_old_traces_handles_missing_dir(self):
        self.mod.TRACE_DIR = "/no/such/dir"
        self.mod.cleanup_old_traces()  # no-op, no crash

    def test_main_writes_state_and_trace(self):
        payload = {"_meta": {"pulse_idx": 3, "generated_at": "2026-05-28T12:00:00Z"}}
        sys.stdin = io.StringIO(json.dumps(payload))
        self.mod.main()
        self.assertTrue(Path(self.mod.STATE_PATH).exists())
        self.assertTrue(any(Path(self.mod.TRACE_DIR).glob("pulse-*.md")))

    def test_main_handles_missing_generated_at(self):
        payload = {"_meta": {"pulse_idx": 4}}
        sys.stdin = io.StringIO(json.dumps(payload))
        self.mod.main()
        self.assertTrue(Path(self.mod.STATE_PATH).exists())

    def test_main_swallows_trace_failure(self):
        payload = {"_meta": {"pulse_idx": 5, "generated_at": "broken-date"}}
        sys.stdin = io.StringIO(json.dumps(payload))
        # write_trace will be called with a bogus date — patch to raise.
        with mock.patch.object(self.mod, "write_trace", side_effect=Exception("boom")):
            captured_err = io.StringIO()
            with mock.patch("sys.stderr", captured_err):
                self.mod.main()
        # State written despite trace failure.
        self.assertTrue(Path(self.mod.STATE_PATH).exists())
        self.assertIn("trace dump failed", captured_err.getvalue())


# ─── actions-ledger ─────────────────────────────────────────────────────────

class ActionsLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "actions-ledger.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _ns(self, **k):
        ns = argparse.Namespace(**k)
        return ns

    def _capture(self, fn, ns) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = fn(ns)
        return rc, out.getvalue(), err.getvalue()

    def test_now_iso(self):
        s = self.mod.now_iso()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_append_writes_jsonl_line(self):
        ns = self._ns(pulse_idx=1, key="k", kind="send", ws_ref="workspace:1",
                       td="td-1", evidence="ev", outcome="verified",
                       verified_via="transcript_size_delta",
                       proof='{"delta": 42}', verdict='{"v": "active"}')
        rc, out, _ = self._capture(self.mod.cmd_append, ns)
        self.assertEqual(rc, 0)
        line = self.mod.LEDGER_PATH.read_text().strip()
        d = json.loads(line)
        self.assertEqual(d["key"], "k")
        self.assertEqual(d["proof"], {"delta": 42})
        self.assertEqual(d["verdict"], {"v": "active"})

    def test_append_proof_and_verdict_fall_back_to_string(self):
        ns = self._ns(pulse_idx=1, key="k", kind="send", ws_ref=None, td=None,
                       evidence="", outcome="verified",
                       verified_via=None, proof="not-json", verdict="not-json")
        rc, _, _ = self._capture(self.mod.cmd_append, ns)
        self.assertEqual(rc, 0)
        d = json.loads(self.mod.LEDGER_PATH.read_text().strip())
        self.assertEqual(d["proof"], "not-json")
        self.assertEqual(d["verdict"], "not-json")

    def test_tail_empty_ledger(self):
        rc, out, _ = self._capture(self.mod.cmd_tail,
                                    self._ns(n=10, ws=None, td=None))
        self.assertEqual(rc, 0)
        self.assertIn("ledger empty", out)

    def test_tail_filters_by_ws_and_td(self):
        # Append two entries.
        for ws, td in [("workspace:1", "td-1"), ("workspace:2", "td-2")]:
            self._capture(self.mod.cmd_append,
                           self._ns(pulse_idx=1, key=f"k-{ws}", kind="send",
                                    ws_ref=ws, td=td, evidence="", outcome="verified",
                                    verified_via=None, proof=None, verdict=None))
        # Filter ws.
        rc, out, _ = self._capture(self.mod.cmd_tail,
                                    self._ns(n=10, ws="workspace:1", td=None))
        self.assertIn("workspace:1", out)
        self.assertNotIn("workspace:2", out)
        # Filter td.
        rc, out, _ = self._capture(self.mod.cmd_tail,
                                    self._ns(n=10, ws=None, td="td-2"))
        self.assertIn("td-2", out)
        self.assertNotIn("td-1", out)

    def test_tail_renders_corrupt_lines_verbatim(self):
        self.mod.LEDGER_PATH.write_text("not json line\n")
        rc, out, _ = self._capture(self.mod.cmd_tail,
                                    self._ns(n=10, ws=None, td=None))
        self.assertIn("not json line", out)

    def test_tail_flags_screen_read(self):
        self._capture(self.mod.cmd_append,
                       self._ns(pulse_idx=1, key="k", kind="send", ws_ref="workspace:1",
                                td=None, evidence="", outcome="verified",
                                verified_via="screen_read", proof=None, verdict=None))
        rc, out, _ = self._capture(self.mod.cmd_tail,
                                    self._ns(n=10, ws=None, td=None))
        # The leading "!" is a flag char in cmd_tail.
        self.assertIn("!screen_read", out)

    def test_grep_finds_pattern(self):
        self.mod.LEDGER_PATH.write_text(
            json.dumps({"key": "alpha"}) + "\n" +
            json.dumps({"key": "beta"}) + "\n"
        )
        rc, out, _ = self._capture(self.mod.cmd_grep,
                                    self._ns(pattern="alpha"))
        self.assertIn("alpha", out)
        self.assertNotIn("beta", out)

    def test_grep_missing_ledger_no_op(self):
        rc, out, _ = self._capture(self.mod.cmd_grep, self._ns(pattern="x"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_rotate_skips_when_below_threshold(self):
        self.mod.LEDGER_PATH.write_text("small")
        rc, out, _ = self._capture(self.mod.cmd_rotate, self._ns())
        self.assertEqual(rc, 0)
        self.assertIn("no rotation", out)

    def test_rotate_rotates_when_over_threshold(self):
        # Lower the threshold for the test, write some content, rotate.
        self.mod.ROTATE_BYTES = 10
        self.mod.LEDGER_PATH.write_text("x" * 50)
        rc, out, _ = self._capture(self.mod.cmd_rotate, self._ns())
        self.assertEqual(rc, 0)
        # Original ledger truncated, gzip archive exists nearby.
        self.assertEqual(self.mod.LEDGER_PATH.read_text(), "")
        archives = list(self.mod.LEDGER_PATH.parent.glob("actions-ledger.*.jsonl.gz"))
        self.assertTrue(archives)

    def test_rotate_no_ledger(self):
        if self.mod.LEDGER_PATH.exists():
            self.mod.LEDGER_PATH.unlink()
        rc, out, _ = self._capture(self.mod.cmd_rotate, self._ns())
        self.assertIn("no ledger", out)

    def test_main_dispatches_subcommand(self):
        sys.argv = ["actions-ledger.py", "tail", "--n", "5"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = self.mod.main()
        self.assertEqual(rc, 0)


# ─── transcript-tail ────────────────────────────────────────────────────────

class TranscriptTailTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "transcript-tail.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_find_via_world_returns_match(self):
        Path(self.mod.WORLD).write_text(json.dumps({
            "live_sessions": [{"ws_ref": "workspace:5", "transcript_path": "/x.jsonl"}]
        }))
        self.assertEqual(self.mod.find_via_world("workspace:5"), "/x.jsonl")

    def test_find_via_world_returns_none_when_missing(self):
        self.assertIsNone(self.mod.find_via_world("workspace:99"))

    def test_find_via_world_handles_corrupt_json(self):
        Path(self.mod.WORLD).write_text("{ bad")
        self.assertIsNone(self.mod.find_via_world("workspace:1"))

    def test_find_via_registry_returns_most_recent(self):
        Path(self.mod.REGISTRY).write_text(json.dumps({
            "tab1": {"cwd": "/x", "transcript_path": "/old.jsonl",
                      "started_at": "2026-01-01T00:00:00Z"},
            "tab2": {"cwd": "/x", "transcript_path": "/new.jsonl",
                      "started_at": "2026-05-01T00:00:00Z"},
            "tab3": {"cwd": "/y", "transcript_path": "/other.jsonl"},
        }))
        self.assertEqual(self.mod.find_via_registry("/x"), "/new.jsonl")

    def test_find_via_registry_returns_none_when_no_match(self):
        Path(self.mod.REGISTRY).write_text(json.dumps({}))
        self.assertIsNone(self.mod.find_via_registry("/no/match"))

    def test_find_via_registry_handles_corrupt_json(self):
        Path(self.mod.REGISTRY).write_text("{ bad")
        self.assertIsNone(self.mod.find_via_registry("/x"))

    def test_read_tail_extracts_last_messages(self):
        path = self._tmp / "t.jsonl"
        path.write_text("\n".join([
            json.dumps({"timestamp": "2026-01-01", "message": {
                "role": "user", "content": "hello"}}),
            json.dumps({"timestamp": "2026-01-02", "message": {
                "role": "assistant", "content": [
                    {"type": "text", "text": "hi back"}]}}),
        ]))
        last_user, last_assistant = self.mod.read_tail(str(path), 100000)
        self.assertEqual(last_user["text"], "hello")
        self.assertEqual(last_assistant["text"], "hi back")

    def test_read_tail_skips_empty_user_text(self):
        # Tool-result user records have no text — should not overwrite the
        # real last_user (a slash command, etc).
        path = self._tmp / "t.jsonl"
        path.write_text("\n".join([
            json.dumps({"timestamp": "t1", "message": {
                "role": "user", "content": [{"type": "text", "text": "/cleanup"}]}}),
            json.dumps({"timestamp": "t2", "message": {
                "role": "user", "content": [{"type": "tool_result", "content": "out"}]}}),
        ]))
        last_user, _ = self.mod.read_tail(str(path), 100000)
        self.assertEqual(last_user["text"], "/cleanup")

    def test_read_tail_preserves_timestamp_when_no_text(self):
        # All user records are tool-results → preserve ts even with empty text.
        path = self._tmp / "t.jsonl"
        path.write_text(json.dumps({
            "timestamp": "ts-only", "message": {
                "role": "user", "content": [{"type": "tool_result", "content": "x"}]}}))
        last_user, _ = self.mod.read_tail(str(path), 100000)
        self.assertEqual(last_user["text"], "")
        self.assertEqual(last_user["ts"], "ts-only")

    def test_read_tail_handles_byte_window_clip(self):
        # A path bigger than n_bytes should still parse the tail.
        path = self._tmp / "t.jsonl"
        # Emit lots of small lines then one big one we want to find.
        body = "\n".join([
            json.dumps({"message": {"role": "user", "content": "early-x"}})
            for _ in range(50)
        ]) + "\n" + json.dumps({"message": {"role": "user", "content": "tail-marker"}}) + "\n"
        path.write_text(body)
        last_user, _ = self.mod.read_tail(str(path), 200)
        self.assertEqual(last_user["text"], "tail-marker")

    def test_read_tail_skips_invalid_lines_and_non_dict_messages(self):
        path = self._tmp / "t.jsonl"
        path.write_text("\n".join([
            "not json",
            json.dumps({"message": "string-not-dict"}),
            json.dumps({"message": {"role": "user", "content": "ok"}}),
        ]))
        last_user, _ = self.mod.read_tail(str(path), 100000)
        self.assertEqual(last_user["text"], "ok")

    def test_main_emits_payload(self):
        path = self._tmp / "t.jsonl"
        path.write_text(json.dumps({
            "timestamp": "t1", "message": {"role": "user", "content": "hi"}}))
        Path(self.mod.WORLD).write_text(json.dumps({
            "live_sessions": [{"ws_ref": "workspace:5",
                               "transcript_path": str(path)}]
        }))
        sys.argv = ["transcript-tail.py", "--ws", "workspace:5"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.mod.main()
        d = json.loads(out.getvalue())
        self.assertEqual(d["last_user"]["text"], "hi")

    def test_main_falls_back_to_closed_cwd(self):
        path = self._tmp / "t.jsonl"
        path.write_text(json.dumps({
            "timestamp": "t1", "message": {"role": "user", "content": "hi"}}))
        Path(self.mod.REGISTRY).write_text(json.dumps({
            "tab1": {"cwd": "/x", "transcript_path": str(path)}
        }))
        sys.argv = ["transcript-tail.py", "--closed-cwd", "/x"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.mod.main()
        d = json.loads(out.getvalue())
        self.assertEqual(d["last_user"]["text"], "hi")

    def test_main_exits_with_error_when_not_found(self):
        sys.argv = ["transcript-tail.py", "--ws", "workspace:nope"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("transcript_not_found", out.getvalue())


# ─── assistant-curator ──────────────────────────────────────────────────────

class AssistantCuratorTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "assistant-curator.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _capture_main(self, argv) -> tuple[int, str, str]:
        sys.argv = ["assistant-curator.py"] + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = self.mod.main()
        return rc, out.getvalue(), err.getvalue()

    def test_today_returns_iso_date(self):
        s = self.mod.today()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}$")

    def test_slugify_basic(self):
        self.assertEqual(self.mod.slugify("Hello World"), "hello-world")

    def test_slugify_strips_non_alnum(self):
        s = self.mod.slugify("foo/bar_baz!!!")
        self.assertNotIn("/", s)
        self.assertNotIn("!", s)

    def test_write_creates_file_and_appends_block(self):
        rc, out, err = self._capture_main([
            "write",
            "--trigger", "When X happens",
            "--rule", "Do Y",
            "--scope", "global",
            "--slug", "test-rule",
        ])
        self.assertEqual(rc, 0, err)
        text = self.mod.CLAUDE_MD.read_text()
        self.assertIn("test-rule", text)
        self.assertIn("Do Y", text)

    def test_write_rejects_invalid_scope(self):
        rc, _, err = self._capture_main([
            "write",
            "--trigger", "x",
            "--rule", "y",
            "--scope", "made-up-scope",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("not in", err)

    def test_write_rejects_blank_trigger(self):
        rc, _, err = self._capture_main([
            "write",
            "--trigger", "",
            "--rule", "y",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("required", err)

    def test_list_renders_lessons(self):
        # First, write a lesson.
        self._capture_main([
            "write", "--trigger", "T", "--rule", "R",
            "--scope", "global", "--slug", "alpha"
        ])
        rc, out, _ = self._capture_main(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("alpha", out)

    def test_list_empty_file(self):
        rc, out, _ = self._capture_main(["list"])
        self.assertEqual(rc, 0)

    def test_rm_removes_block(self):
        self._capture_main([
            "write", "--trigger", "T", "--rule", "R",
            "--scope", "global", "--slug", "to-remove"
        ])
        rc, out, _ = self._capture_main(["rm", "to-remove"])
        self.assertEqual(rc, 0)
        text = self.mod.CLAUDE_MD.read_text()
        self.assertNotIn("to-remove", text)

    def test_rm_unknown_slug(self):
        rc, out, err = self._capture_main(["rm", "no-such-slug"])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
