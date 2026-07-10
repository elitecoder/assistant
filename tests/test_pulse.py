"""Unit tests for bin/pulse.py — exercise the mechanical bits.

We DON'T spawn claude here. The Observer call is mocked via a sandboxed
HOME and a dummy claude binary that emits a verdict JSON. Everything
else (inbox drain, verdict→action lookup, NO_INGEST_GUARD, awaiting card
emission, back-off respect) is straight Python and runs in <2s.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PULSE_PATH = REPO / "bin/pulse.py"


def load_pulse(home: Path):
    """Import bin/pulse.py with HOME pointed at a tempdir.

    Each call returns a fresh module bound to the tempdir's HOME — every
    HOME-dependent constant (ASSISTANT_DIR, INBOX_DIR, ...) is computed
    at module import, so each test gets clean paths."""
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


def seed_receipt(home: Path, ws_ref: str, summary: str = "shipped, CI green") -> Path:
    """Drop a work receipt for ws_ref so the pre-cleanup gate passes. Matches
    write-receipt.py's slug + filename convention (workspace:N -> workspace-N)."""
    rdir = home / ".assistant/receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    p = rdir / f"{ws_ref.replace(':', '-')}-1700000000.json"
    p.write_text(json.dumps({
        "ts": "2026-06-06T00:00:00Z", "ws_ref": ws_ref, "summary": summary,
        "ci_status": "green", "reviewer_approved": True, "outcome": "shipped",
        "quality_score": "high",
    }))
    return p


class DrainInboxTests(unittest.TestCase):
    """drain_inbox is the typed event-spine step (Keel M1): inbox drops become
    WorldEvent rows in ~/.assistant/events.jsonl instead of being unlinked
    unread. The spine's own behavior (dedup, quarantine, lock, crash events)
    is covered in test_eventspine.py — here we pin the pulse-facing contract:
    the count, the unlink, and that a broken spine never breaks the pulse."""

    def test_consumes_inbox_drops_into_events_jsonl(self):
        with TemporaryDirectory() as tmp:
            tmp = fixture_home(Path(tmp))
            inbox = tmp / ".assistant/inbox"
            (inbox / "pulse-1.json").write_text("{}")   # legacy wake ping
            (inbox / "cmux-workspace-7-1.json").write_text(json.dumps({
                "ts": "2026-07-09T12:00:00Z", "event": "workspace_signal",
                "ws_ref": "workspace:7", "signal_type": "needs_input",
                "pattern_matched": "Notification", "screen_snippet": "?"}))
            (inbox / "other.txt").write_text("keep")
            mod = load_pulse(tmp)
            self.assertEqual(mod.drain_inbox(), 2)
            self.assertFalse((inbox / "pulse-1.json").exists())
            self.assertFalse((inbox / "cmux-workspace-7-1.json").exists())
            self.assertTrue((inbox / "other.txt").exists())
            rows = [json.loads(l) for l in
                    (tmp / ".assistant/events.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["schema"] == "world-event/1" for r in rows))

    def test_missing_inbox_dir_returns_zero(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)  # no .assistant subdir
            mod = load_pulse(tmp)
            self.assertEqual(mod.drain_inbox(), 0)

    def test_spine_failure_never_breaks_the_pulse(self):
        # A raising spine is logged and swallowed; the inbox is left intact
        # for the next pulse.
        with TemporaryDirectory() as tmp:
            tmp = fixture_home(Path(tmp))
            (tmp / ".assistant/inbox/pulse-1.json").write_text("{}")
            mod = load_pulse(tmp)
            sys.path.insert(0, str(REPO / "src"))
            from assistant import eventspine
            with mock.patch.object(eventspine, "drain_typed_inbox",
                                   side_effect=RuntimeError("boom")):
                self.assertEqual(mod.drain_inbox(), 0)
            self.assertTrue((tmp / ".assistant/inbox/pulse-1.json").exists())


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

    def test_ready_for_cleanup_sends_when_assistant_merged(self):
        # Pre-seed the merge ledger AND a work receipt so both /cleanup gates
        # open (Assistant-merged + receipt-exists).
        self.mod.record_assistant_merge("workspace:99", ["10000"])
        seed_receipt(self._tmp, "workspace:99")
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

    def test_ready_for_cleanup_blocked_when_no_merge_record(self):
        # No merge ledger entry → /cleanup must NOT fire. The verdict gets
        # downgraded to an awaiting card so the user can decide.
        sent = []
        awaiting = []
        with mock.patch.object(self.mod, "cmux_send",
                               lambda *a, **k: sent.append(a) or {}):
            action = self.mod.execute_verdict(
                self._ws(),
                {"verdict": "ready_for_cleanup",
                 "summary": "audit complete; no PR needed",
                 "next": "send /cleanup"},
                awaiting,
            )
        self.assertEqual(sent, [], "/cleanup must NOT be sent without merge record")
        self.assertEqual(action["kind"], "emit-card")
        self.assertEqual(len(awaiting), 1)
        self.assertIn("confirm /cleanup", awaiting[0]["title"].lower())
        self.assertIn("audit complete", awaiting[0]["detail"])
        self.assertEqual(awaiting[0]["ws_ref"], "workspace:99")

    def test_ready_for_cleanup_per_workspace_merge_record(self):
        # Merge record for ws:42 must NOT open the gate for ws:99.
        self.mod.record_assistant_merge("workspace:42", ["1234"])
        sent = []
        awaiting = []
        with mock.patch.object(self.mod, "cmux_send",
                               lambda *a, **k: sent.append(a) or {}):
            action = self.mod.execute_verdict(
                self._ws("workspace:99"),
                {"verdict": "ready_for_cleanup", "summary": "s", "next": "n"},
                awaiting,
            )
        self.assertEqual(sent, [])
        self.assertEqual(action["kind"], "emit-card")

    def test_ready_for_merge_records_merge_ledger_on_success(self):
        # /merge-when-ready dispatch must mark the workspace eligible for
        # future auto-/cleanup.
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
        self.assertTrue(self.mod.assistant_merged_workspace("workspace:99"))

    def test_ready_for_merge_does_not_record_on_failure(self):
        # If cmux_send fails, do NOT poison the merge ledger.
        with mock.patch.object(self.mod, "cmux_send",
                               lambda *a, **k: {"outcome": "failed"}):
            with mock.patch.object(self.mod, "previous_send_ingested", lambda *a: True):
                action = self.mod.execute_verdict(
                    self._ws(), {"verdict": "ready_for_merge", "summary": "s", "next": "n"}, [],
                )
        self.assertEqual(action["outcome"], "failed")
        self.assertFalse(self.mod.assistant_merged_workspace("workspace:99"))

    def test_assistant_merged_workspace_silent_on_missing_ledger(self):
        # No ledger file at all → False, not crash.
        self.assertFalse(self.mod.assistant_merged_workspace("workspace:99"))

    def test_assistant_merged_workspace_silent_on_corrupt_lines(self):
        self.mod.MERGE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.MERGE_LEDGER_PATH.write_text("{ not json\n")
        # Should NOT raise; just returns False because no valid record matched.
        self.assertFalse(self.mod.assistant_merged_workspace("workspace:99"))


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
        # Seed merge ledger + receipt so both /cleanup gates open — we want this
        # test to exercise NO_INGEST_GUARD specifically, not the cleanup gates.
        self.mod.record_assistant_merge("workspace:7", ["123"])
        seed_receipt(self._tmp, "workspace:7")
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


class TimeHelpersTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_utc_iso_format(self):
        s = self.mod.utc_iso()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_utc_ts_is_int(self):
        v = self.mod.utc_ts()
        self.assertIsInstance(v, int)
        self.assertGreater(v, 1_700_000_000)


class LoadBedrockEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        fixture_home(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_returns_empty_when_zprofile_missing(self):
        # No ~/.zprofile in tempdir. Loader should return {} and not crash.
        mod = load_pulse(self._tmp)
        self.assertEqual(mod.load_bedrock_env(), {})

    def test_extracts_known_keys(self):
        zprofile = self._tmp / ".zprofile"
        zprofile.write_text(textwrap.dedent("""\
            # comment line
            export PATH="/usr/bin:$PATH"
            export CLAUDE_CODE_USE_BEDROCK=1
            export AWS_REGION="us-west-2"
            export AWS_BEARER_TOKEN_BEDROCK='SECRET-TOKEN-VALUE'
            export OTHER_VAR=ignored
            export ANTHROPIC_API_KEY=key123
            export AWS_PROFILE=default
        """))
        mod = load_pulse(self._tmp)
        env = mod.load_bedrock_env()
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(env["AWS_REGION"], "us-west-2")
        self.assertEqual(env["AWS_BEARER_TOKEN_BEDROCK"], "SECRET-TOKEN-VALUE")
        self.assertEqual(env["AWS_PROFILE"], "default")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "key123")
        self.assertNotIn("OTHER_VAR", env)
        self.assertNotIn("PATH", env)


class RunSubprocessTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_returns_rc_stdout_stderr_on_success(self):
        rc, out, err = self.mod.run([sys.executable, "-c", "print('hello')"])
        self.assertEqual(rc, 0)
        self.assertIn("hello", out)
        self.assertEqual(err, "")

    def test_captures_stderr(self):
        rc, _, err = self.mod.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("boom", err)

    def test_timeout_returns_124(self):
        rc, _, err = self.mod.run(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=1
        )
        self.assertEqual(rc, 124)
        self.assertIn("timeout", err)

    def test_unraisable_subprocess_exception_returns_1(self):
        rc, _, err = self.mod.run(["/no/such/binary/anywhere"])
        self.assertEqual(rc, 1)
        self.assertTrue(err)

    def test_merge_bedrock_layers_env_only_if_unset(self):
        # Set the cached env so we have something to merge.
        self.mod._BEDROCK_ENV = {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-west-2",
        }
        # User-supplied env wins (setdefault semantics).
        explicit = {"AWS_REGION": "eu-west-1", "PATH": "/x"}
        rc, out, err = self.mod.run(
            [sys.executable, "-c",
             "import os; print(os.environ.get('AWS_REGION'), os.environ.get('CLAUDE_CODE_USE_BEDROCK'))"],
            env=explicit, merge_bedrock=True,
        )
        self.assertEqual(rc, 0)
        self.assertIn("eu-west-1", out)
        # CLAUDE_CODE_USE_BEDROCK was not in explicit; bedrock cache filled it in.
        self.assertIn("1", out)


class AppendLedgerAndLoadStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_append_ledger_creates_dir_and_writes_jsonl(self):
        # Ledger path under .assistant/ — already exists. Write two entries.
        self.mod.append_ledger({"a": 1})
        self.mod.append_ledger({"b": 2})
        lines = (self._tmp / ".assistant/actions-ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), {"a": 1})
        self.assertEqual(json.loads(lines[1]), {"b": 2})

    def test_load_state_returns_default_when_missing(self):
        s = self.mod.load_state()
        self.assertEqual(s["_meta"]["pulse_idx"], 0)
        self.assertEqual(s["actions_taken"], [])
        self.assertEqual(s["awaiting_input"], [])

    def test_load_state_reads_existing_file(self):
        path = self._tmp / ".claude/cache/assistant-state.json"
        path.write_text(json.dumps({"_meta": {"pulse_idx": 42}}))
        s = self.mod.load_state()
        self.assertEqual(s["_meta"]["pulse_idx"], 42)

    def test_load_state_handles_corrupt_json(self):
        path = self._tmp / ".claude/cache/assistant-state.json"
        path.write_text("{ this is not json")
        s = self.mod.load_state()
        self.assertEqual(s["_meta"]["pulse_idx"], 0)


class WriteHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_writes_heartbeat_json_atomically(self):
        self.mod.write_heartbeat(pulse_idx=7, drained=3)
        hb = json.loads((self._tmp / ".assistant/heartbeat.json").read_text())
        self.assertEqual(hb["pulse_idx"], 7)
        self.assertEqual(hb["pulses_drained_this_run"], 3)
        self.assertEqual(hb["model"], "python-mechanical")
        self.assertEqual(hb["status"], "running")
        self.assertEqual(hb["ws_ref"], "(launchd)")
        self.assertIsNotNone(hb["last_pulse_ts"])

    def test_no_tmp_left_behind(self):
        self.mod.write_heartbeat(pulse_idx=1, drained=0)
        # The .tmp file is replaced atomically; only .json should exist.
        self.assertTrue((self._tmp / ".assistant/heartbeat.json").exists())
        self.assertFalse((self._tmp / ".assistant/heartbeat.json.tmp").exists())


class HelperSubprocessWrapperTests(unittest.TestCase):
    """purge-stale-awaiting / pick-ws-batch / build-ctx / cmux-send /
    save-summary / pick-open-todos / dispatch-todo. These are 1:1
    subprocess wrappers — patch self.mod.run and assert the right
    cmdline + parsing happens."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_purge_stale_awaiting_logs_warning_on_failure(self):
        with mock.patch.object(self.mod, "run", return_value=(2, "", "boom")):
            with self.assertLogs("pulse", level="WARNING"):
                self.mod.purge_stale_awaiting()

    def test_purge_stale_awaiting_silent_on_success(self):
        with mock.patch.object(self.mod, "run", return_value=(0, "", "")):
            self.mod.purge_stale_awaiting()  # no logs assertion — silent path

    def test_pick_ws_batch_parses_json(self):
        payload = {"to_reclassify": [{"ref": "workspace:1"}], "reuse_cached": [],
                   "backed_off": [], "total_ws": 1}
        with mock.patch.object(self.mod, "run",
                               return_value=(0, json.dumps(payload), "")):
            out = self.mod.pick_ws_batch()
        self.assertEqual(out["total_ws"], 1)

    def test_pick_ws_batch_returns_default_on_rc(self):
        with mock.patch.object(self.mod, "run", return_value=(1, "", "fail")):
            out = self.mod.pick_ws_batch()
        self.assertEqual(out, {"to_reclassify": [], "reuse_cached": [],
                               "backed_off": [], "total_ws": 0})

    def test_pick_ws_batch_returns_default_on_bad_json(self):
        with mock.patch.object(self.mod, "run",
                               return_value=(0, "not json", "")):
            out = self.mod.pick_ws_batch()
        self.assertEqual(out["total_ws"], 0)

    def test_build_ctx_parses_json(self):
        payload = {"ws_ref": "workspace:1", "transcript_path": None,
                   "agent_status": "idle", "last_turn_age_sec": 30,
                   "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
                   "title": "t", "cwd": "/"}
        with mock.patch.object(self.mod, "run",
                               return_value=(0, json.dumps(payload), "")):
            out = self.mod.build_ctx({"ref": "workspace:1", "title": "t", "cwd": "/"})
        self.assertEqual(out["ws_ref"], "workspace:1")

    def test_build_ctx_returns_none_on_rc(self):
        with mock.patch.object(self.mod, "run", return_value=(1, "", "fail")):
            out = self.mod.build_ctx({"ref": "workspace:1", "title": "t", "cwd": "/"})
        self.assertIsNone(out)

    def test_build_ctx_returns_none_on_bad_json(self):
        with mock.patch.object(self.mod, "run", return_value=(0, "not json", "")):
            out = self.mod.build_ctx({"ref": "workspace:1", "title": "t", "cwd": "/"})
        self.assertIsNone(out)

    def test_save_summary_invokes_save_script(self):
        seen = {}
        def fake_run(cmd, **k):
            seen["cmd"] = cmd
            return (0, "saved", "")
        with mock.patch.object(self.mod, "run", side_effect=fake_run):
            self.mod.save_summary({"ref": "workspace:1", "title": "t", "cwd": "/"},
                                   {"verdict": "active", "summary": "s", "next": "n"})
        self.assertIn("save-ws-summary.py", seen["cmd"][1])
        self.assertIn("--ws-ref", seen["cmd"])
        self.assertIn("workspace:1", seen["cmd"])

    def test_save_summary_logs_on_failure(self):
        with mock.patch.object(self.mod, "run",
                               return_value=(2, "", "rejected: missing next")):
            with self.assertLogs("pulse", level="ERROR"):
                self.mod.save_summary({"ref": "workspace:1", "title": "t", "cwd": "/"},
                                      {"verdict": "active"})

    def test_cmux_send_returns_parsed_record(self):
        rec = {"outcome": "sent", "transcript_size_delta": 1024,
               "target_ws_ref": "workspace:1"}
        with mock.patch.object(self.mod, "run",
                               return_value=(0, json.dumps(rec), "")):
            out = self.mod.cmux_send("workspace:1", "/cleanup")
        self.assertEqual(out["transcript_size_delta"], 1024)

    def test_cmux_send_returns_failed_when_rc_nonzero(self):
        with mock.patch.object(self.mod, "run", return_value=(1, "", "boom")):
            out = self.mod.cmux_send("workspace:1", "/cleanup")
        self.assertEqual(out["outcome"], "failed")
        self.assertEqual(out["transcript_size_delta"], 0)

    def test_cmux_send_handles_unparsed_stdout(self):
        with mock.patch.object(self.mod, "run", return_value=(0, "not json", "")):
            out = self.mod.cmux_send("workspace:1", "/cleanup")
        self.assertEqual(out["outcome"], "ok-unparsed")
        self.assertIsNone(out["transcript_size_delta"])

    def test_pick_open_todos_returns_buckets(self):
        payload = {"bucket_a": [], "bucket_b": [{"id": "td-1", "priority": "P1"}],
                   "bucket_c": [], "totals": {"open": 1}}
        with mock.patch.object(self.mod, "run",
                               return_value=(0, json.dumps(payload), "")):
            out = self.mod.pick_open_todos()
        self.assertEqual(len(out["bucket_b"]), 1)

    def test_pick_open_todos_returns_default_on_rc(self):
        with mock.patch.object(self.mod, "run", return_value=(1, "", "fail")):
            out = self.mod.pick_open_todos()
        self.assertEqual(out["bucket_a"], [])
        self.assertEqual(out["bucket_b"], [])

    def test_pick_open_todos_returns_default_on_bad_json(self):
        with mock.patch.object(self.mod, "run",
                               return_value=(0, "not json", "")):
            out = self.mod.pick_open_todos()
        self.assertEqual(out["bucket_b"], [])

    def test_dispatch_todo_returns_false(self):
        # The current implementation is a no-op stub — assert it returns False
        # so a future implementation flipping it to True is a deliberate change.
        self.assertFalse(self.mod.dispatch_todo("td-7"))


class CallObserverBatchTests(unittest.TestCase):
    """call_observer_batch writes the prompt + ctxs to disk, spawns a
    fake claude that reads the prompt and writes verdicts.jsonl, then
    parses the result. We mock self.mod.run to fake the subprocess but
    still run the disk-IO logic for real."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_empty_batch_returns_empty(self):
        self.assertEqual(self.mod.call_observer_batch([], pulse_idx=1, batch_idx=0),
                         ({}, {}))

    def test_missing_prompt_returns_empty_and_logs(self):
        # Point the prompt path at something that doesn't exist.
        original = self.mod.OBSERVER_BATCH_PROMPT
        self.mod.OBSERVER_BATCH_PROMPT = self._tmp / "missing.md"
        try:
            with self.assertLogs("pulse", level="ERROR"):
                self.assertEqual(
                    self.mod.call_observer_batch(
                        [{"ws_ref": "workspace:1", "title": "t", "cwd": "/"}],
                        pulse_idx=1, batch_idx=0,
                    ),
                    ({}, {}),
                )
        finally:
            self.mod.OBSERVER_BATCH_PROMPT = original

    def test_writes_artifacts_and_reads_verdicts(self):
        # Provide a real batch prompt for the test.
        prompt_file = self._tmp / "obs.md"
        prompt_file.write_text("# fake observer prompt\n")
        original = self.mod.OBSERVER_BATCH_PROMPT
        self.mod.OBSERVER_BATCH_PROMPT = prompt_file
        try:
            ctxs = [{"ws_ref": "workspace:1", "title": "t", "cwd": str(self._tmp)},
                    {"ws_ref": "workspace:2", "title": "u", "cwd": str(self._tmp)}]

            def fake_run(cmd, *, input_text=None, timeout=None, env=None,
                         merge_bedrock=False):
                # Find the run dir from --add-dir, then write verdicts.
                add_dirs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]
                run_dir = next(p for p in add_dirs if "observer-runs" in p)
                vp = Path(run_dir) / "verdicts.jsonl"
                vp.write_text(
                    '{"ws_ref": "workspace:1", "verdict": "active", "summary": "s1", "next": "n1"}\n'
                    '{"ws_ref": "workspace:2", "verdict": "no_action", "summary": "s2", "next": "n2"}\n'
                )
                return (0, "stdout-trail", "")

            with mock.patch.object(self.mod, "run", side_effect=fake_run):
                out, usage = self.mod.call_observer_batch(ctxs, pulse_idx=99, batch_idx=0)
            self.assertEqual(set(out.keys()), {"workspace:1", "workspace:2"})
            self.assertEqual(out["workspace:1"]["verdict"], "active")
            # stdout isn't a --output-format json envelope here, so metering
            # falls back to the chars/4 estimate and says so.
            self.assertEqual(usage["source"], "estimated")
            self.assertGreater(usage["tokens_in"], 0)

            run_dir = self._tmp / ".assistant/observer-runs/0099/batch-0"
            self.assertTrue((run_dir / "prompt.md").exists())
            self.assertTrue((run_dir / "ctxs.json").exists())
            self.assertTrue((run_dir / "verdicts.jsonl").exists())
            self.assertTrue((run_dir / "stdout.txt").exists())
            self.assertTrue((run_dir / "stderr.txt").exists())
            meta = json.loads((run_dir / "meta.json").read_text())
            self.assertEqual(meta["rc"], 0)
            self.assertIn("workspace:1", meta["ws_refs"])
        finally:
            self.mod.OBSERVER_BATCH_PROMPT = original

    def test_observer_cmd_requests_json_output_format(self):
        # Adversarial pin: if `--output-format json` is ever dropped from the
        # Observer cmd, stdout stops being a usage envelope and real token/cost
        # capture silently dies while every other test stays green.
        prompt_file = self._tmp / "obs.md"
        prompt_file.write_text("# fake\n")
        original = self.mod.OBSERVER_BATCH_PROMPT
        self.mod.OBSERVER_BATCH_PROMPT = prompt_file
        try:
            captured = {}

            def fake_run(cmd, *, input_text=None, timeout=None, env=None,
                         merge_bedrock=False):
                captured["cmd"] = cmd
                return (0, "", "")

            with mock.patch.object(self.mod, "run", side_effect=fake_run):
                self.mod.call_observer_batch(
                    [{"ws_ref": "workspace:1", "title": "t", "cwd": str(self._tmp)}],
                    pulse_idx=1, batch_idx=0,
                )
            cmd = captured["cmd"]
            self.assertIn("--output-format", cmd)
            self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
            self.assertIn("--print", cmd)
        finally:
            self.mod.OBSERVER_BATCH_PROMPT = original

    def test_persists_artifacts_even_when_subprocess_fails(self):
        prompt_file = self._tmp / "obs.md"
        prompt_file.write_text("# fake\n")
        original = self.mod.OBSERVER_BATCH_PROMPT
        self.mod.OBSERVER_BATCH_PROMPT = prompt_file
        try:
            with mock.patch.object(self.mod, "run", return_value=(1, "out", "boom")):
                with self.assertLogs("pulse", level="WARNING"):
                    out, _usage = self.mod.call_observer_batch(
                        [{"ws_ref": "workspace:1", "title": "t", "cwd": str(self._tmp)}],
                        pulse_idx=1, batch_idx=0,
                    )
            # No verdicts but the diagnostics should still be on disk.
            self.assertEqual(out, {})
            run_dir = self._tmp / ".assistant/observer-runs/0001/batch-0"
            self.assertEqual((run_dir / "stdout.txt").read_text(), "out")
            self.assertEqual((run_dir / "stderr.txt").read_text(), "boom")
        finally:
            self.mod.OBSERVER_BATCH_PROMPT = original


class MainPipelineTests(unittest.TestCase):
    """End-to-end main() with subprocess wrappers mocked. Exercises the
    full pulse: drain, purge, batch pick, ctx, observer, save, dispatch,
    state-write, heartbeat. No claude subprocess; no real LLM."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _run_main(self, argv):
        sys.argv = ["pulse.py"] + list(argv)
        return self.mod.main()

    def test_dry_run_prints_summary_and_skips_observer(self):
        # pick_ws_batch returns one ws; build_ctx returns a ctx; main() should
        # NOT spawn observer, NOT write state, NOT write heartbeat.
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}],
                "reuse_cached": [], "backed_off": [], "total_ws": 1,
            }):
            with mock.patch.object(self.mod, "build_ctx", return_value={
                    "ws_ref": "workspace:1", "transcript_path": None,
                    "agent_status": "idle", "last_turn_age_sec": 0,
                    "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
                    "title": "t", "cwd": "/"}):
                with mock.patch.object(self.mod, "call_observer_batch") as obs_mock:
                    with mock.patch.object(self.mod, "pick_open_todos",
                                           return_value={"bucket_b": []}):
                        captured = io.StringIO()
                        with mock.patch("sys.stdout", captured):
                            rc = self._run_main(["--dry-run"])
        self.assertEqual(rc, 0)
        obs_mock.assert_not_called()
        body = captured.getvalue()
        self.assertIn("workspace:1", body)
        self.assertIn("dry-run", body)
        # No heartbeat / state file should exist (dry-run skips them).
        self.assertFalse((self._tmp / ".assistant/heartbeat.json").exists())

    def test_real_pulse_writes_heartbeat_and_state(self):
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}],
                "reuse_cached": [], "backed_off": [
                    {"ref": "workspace:9", "reason": "user said so", "title": "x"}],
                "total_ws": 2,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "build_ctx", return_value={
                        "ws_ref": "workspace:1", "transcript_path": None,
                        "agent_status": "idle", "last_turn_age_sec": 0,
                        "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
                        "title": "t", "cwd": "/"}):
                    with mock.patch.object(
                            self.mod, "call_observer_batch",
                            return_value=({"workspace:1": {
                                "ws_ref": "workspace:1", "verdict": "active",
                                "summary": "s", "next": "n"}}, {})):
                        with mock.patch.object(self.mod, "save_summary"):
                            with mock.patch.object(self.mod, "pick_open_todos",
                                                   return_value={"bucket_b": []}):
                                with mock.patch.object(
                                        self.mod, "run",
                                        return_value=(0, "", "")):
                                    rc = self._run_main([])
        self.assertEqual(rc, 0)
        # Heartbeat written.
        hb = json.loads((self._tmp / ".assistant/heartbeat.json").read_text())
        self.assertEqual(hb["model"], "python-mechanical")

    def test_drains_inbox_and_records_count(self):
        # Drop two inbox files; main should delete both.
        inbox = self._tmp / ".assistant/inbox"
        (inbox / "pulse-1.json").write_text("{}")
        (inbox / "pulse-2.json").write_text("{}")
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "pick_open_todos",
                                       return_value={"bucket_b": []}):
                    with mock.patch.object(self.mod, "run",
                                           return_value=(0, "", "")):
                        rc = self._run_main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(inbox.glob("pulse-*.json"))), 0)

    def test_build_ctx_failure_recorded_as_skipped(self):
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}],
                "reuse_cached": [], "backed_off": [], "total_ws": 1,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "build_ctx", return_value=None):
                    with mock.patch.object(self.mod, "call_observer_batch",
                                           return_value=({}, {})) as obs_mock:
                        with mock.patch.object(self.mod, "pick_open_todos",
                                               return_value={"bucket_b": []}):
                            with mock.patch.object(self.mod, "run",
                                                   return_value=(0, "", "")):
                                rc = self._run_main([])
        self.assertEqual(rc, 0)
        # build-ctx returned None → main treats as skipped, never calls observer.
        # No verdict to save; observer batch CALLED with empty list (still wraps
        # the ThreadPoolExecutor branch).
        # State-write was invoked via run() — exercise the success path.

    def test_observer_returns_no_verdict_for_ws_records_skipped(self):
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}],
                "reuse_cached": [], "backed_off": [], "total_ws": 1,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "build_ctx", return_value={
                        "ws_ref": "workspace:1", "transcript_path": None,
                        "agent_status": "idle", "last_turn_age_sec": 0,
                        "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
                        "title": "t", "cwd": "/"}):
                    # Observer returns empty dict — no verdict for ws:1.
                    with mock.patch.object(self.mod, "call_observer_batch",
                                           return_value=({}, {})):
                        with mock.patch.object(self.mod, "save_summary") as save_mock:
                            with mock.patch.object(self.mod, "pick_open_todos",
                                                   return_value={"bucket_b": []}):
                                with mock.patch.object(self.mod, "run",
                                                       return_value=(0, "", "")):
                                    rc = self._run_main([])
        self.assertEqual(rc, 0)
        # When Observer returns no verdict, a synthetic "active" summary is saved
        # so the dashboard stays fresh rather than going stale on the prior verdict.
        save_mock.assert_called_once()
        ws_arg, verdict_arg = save_mock.call_args[0]
        self.assertEqual(verdict_arg["verdict"], "active")

    def test_dispatch_cap_hit_when_over_active_limit(self):
        # Set up: 1 ws to_reclassify, ws_meta would have agent_status=working.
        # bucket_b non-empty. Cap is 5 active OR 30 total. Make total >= 30
        # to trigger the cap-hit branch.
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}],
                "reuse_cached": [], "backed_off": [], "total_ws": 35,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "build_ctx", return_value={
                        "ws_ref": "workspace:1", "transcript_path": None,
                        "agent_status": "working", "last_turn_age_sec": 0,
                        "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
                        "title": "t", "cwd": "/"}):
                    with mock.patch.object(self.mod, "call_observer_batch",
                                           return_value=({"workspace:1": {
                                               "ws_ref": "workspace:1", "verdict": "active",
                                               "summary": "s", "next": "n"}}, {})):
                        with mock.patch.object(self.mod, "save_summary"):
                            with mock.patch.object(self.mod, "pick_open_todos",
                                                   return_value={"bucket_b": [
                                                       {"id": "td-1", "priority": "P1"}]}):
                                with mock.patch.object(
                                        self.mod, "run",
                                        return_value=(0, "", "")) as run_mock:
                                    rc = self._run_main([])
        self.assertEqual(rc, 0)
        # state-write should have been called once with a payload containing
        # the dispatch-cap-hit action.
        saved = [c for c in run_mock.call_args_list
                 if c.args and "state-write.py" in str(c.args[0])]
        self.assertTrue(saved)
        payload = json.loads(saved[-1].kwargs.get("input_text", "{}"))
        keys = {a.get("key") for a in payload.get("actions_taken", [])}
        self.assertTrue(any("dispatch-cap-hit" in k for k in keys))

    def test_dispatch_attempted_when_under_caps(self):
        # n_active=0, total=1, bucket_b has 3 items (we should attempt up to 2).
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 1,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "pick_open_todos",
                                       return_value={"bucket_b": [
                                           {"id": "td-3", "priority": "P3"},
                                           {"id": "td-1", "priority": "P1"},
                                           {"id": "td-2", "priority": "P2"},
                                       ]}):
                    with mock.patch.object(self.mod, "dispatch_todo",
                                           return_value=False) as disp_mock:
                        with mock.patch.object(self.mod, "run",
                                               return_value=(0, "", "")):
                            rc = self._run_main([])
        self.assertEqual(rc, 0)
        # Should attempt up to MAX_DISPATCH_PER_PULSE (2), highest priority first.
        self.assertEqual(disp_mock.call_count, self.mod.MAX_DISPATCH_PER_PULSE)
        called_ids = [c.args[0] for c in disp_mock.call_args_list]
        self.assertEqual(called_ids[0], "td-1")  # P1 first
        self.assertEqual(called_ids[1], "td-2")

    def test_dispatch_success_records_verified_action(self):
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "pick_open_todos",
                                       return_value={"bucket_b": [
                                           {"id": "td-1", "priority": "P0"}]}):
                    with mock.patch.object(self.mod, "dispatch_todo",
                                           return_value=True):
                        with mock.patch.object(
                                self.mod, "run",
                                return_value=(0, "", "")) as run_mock:
                            rc = self._run_main([])
        self.assertEqual(rc, 0)
        saved = [c for c in run_mock.call_args_list
                 if c.args and "state-write.py" in str(c.args[0])]
        payload = json.loads(saved[-1].kwargs.get("input_text", "{}"))
        kinds = {a.get("kind") for a in payload.get("actions_taken", [])}
        self.assertIn("dispatch", kinds)

    # ── metering wiring ──────────────────────────────────────────────────
    # These read the ACTUAL metrics.jsonl the pulse appended — pinning the
    # main() wiring, not just the metering module's unit behavior.

    CTX_WS1 = {"ws_ref": "workspace:1", "transcript_path": None,
               "agent_status": "idle", "last_turn_age_sec": 0,
               "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
               "title": "t", "cwd": "/"}

    def _metrics_records(self):
        p = self._tmp / ".assistant/metrics.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def _plant_summary(self, ws_ref, verdict):
        p = (self._tmp / ".assistant/observer-summaries"
             / f"{ws_ref.replace(':', '_')}.json")
        p.write_text(json.dumps({"ws_ref": ws_ref, "verdict": verdict}))

    def _run_metered_pulse(self, observer_result):
        """One real (non-dry-run) pulse with subprocesses mocked and the
        Observer batch returning `observer_result` = (verdicts_by_ref, usage)."""
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}],
                "reuse_cached": [], "backed_off": [], "total_ws": 1,
            }), \
             mock.patch.object(self.mod, "purge_stale_awaiting"), \
             mock.patch.object(self.mod, "build_ctx", return_value=dict(self.CTX_WS1)), \
             mock.patch.object(self.mod, "call_observer_batch",
                               return_value=observer_result), \
             mock.patch.object(self.mod, "save_summary"), \
             mock.patch.object(self.mod, "pick_open_todos",
                               return_value={"bucket_b": []}), \
             mock.patch.object(self.mod, "run", return_value=(0, "", "")):
            return self._run_main([])

    def test_pulse_appends_metrics_record(self):
        # The existing pipeline tests never read metrics.jsonl — this one does.
        usage = {"tokens_in": 100, "tokens_out": 10, "cost_usd": 0.01, "source": "cli"}
        rc = self._run_metered_pulse((
            {"workspace:1": {"ws_ref": "workspace:1", "verdict": "active",
                             "summary": "s", "next": "n"}}, usage))
        self.assertEqual(rc, 0)
        recs = self._metrics_records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertTrue(rec["observer_called"])
        self.assertEqual(rec["batch_size"], 1)
        self.assertEqual(rec["pulse_idx"], 1)
        self.assertEqual(rec["verdicts"], {"active": 1})
        self.assertEqual(rec["verdict_changes"], 0)  # no prior summary on disk
        self.assertEqual(rec["synthesized"], 0)
        self.assertEqual(rec["usage_source"], "cli")
        self.assertEqual(rec["tokens_in"], 100)
        self.assertEqual(rec["cost_usd_est"], 0.01)

    def test_synthesized_verdict_not_counted_as_change(self):
        # Prior verdict on disk is needs_user; Observer batch fails → pulse
        # synthesizes "active". That is a failure artifact, NOT a change:
        # verdict_changes stays 0 and the synthesized counter records it.
        self._plant_summary("workspace:1", "needs_user")
        rc = self._run_metered_pulse(({}, {}))
        self.assertEqual(rc, 0)
        recs = self._metrics_records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["verdicts"], {"active": 1})
        self.assertEqual(rec["verdict_changes"], 0)
        self.assertEqual(rec["synthesized"], 1)

    def test_real_verdict_change_still_counted(self):
        # Non-vacuous counterpart: a REAL Observer verdict that differs from
        # the prior one still counts, and synthesized stays 0.
        self._plant_summary("workspace:1", "needs_user")
        rc = self._run_metered_pulse((
            {"workspace:1": {"ws_ref": "workspace:1", "verdict": "active",
                             "summary": "s", "next": "n"}}, {}))
        self.assertEqual(rc, 0)
        rec = self._metrics_records()[0]
        self.assertEqual(rec["verdict_changes"], 1)
        self.assertEqual(rec["synthesized"], 0)

    def test_prev_snapshot_failure_degrades_comparison_only(self):
        # load_prev_verdicts blowing up must null the comparison, not drop the
        # whole record — cost/usage for the pulse is still written.
        sys.path.insert(0, str(REPO / "bin"))
        import metering as metering_mod  # same module object main() imports
        usage = {"tokens_in": 100, "tokens_out": 10, "cost_usd": 0.01, "source": "cli"}
        with mock.patch.object(metering_mod, "load_prev_verdicts",
                               side_effect=OSError("summaries dir unreadable")):
            with self.assertLogs("pulse", level="WARNING"):
                rc = self._run_metered_pulse((
                    {"workspace:1": {"ws_ref": "workspace:1", "verdict": "active",
                                     "summary": "s", "next": "n"}}, usage))
        self.assertEqual(rc, 0)
        recs = self._metrics_records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertIsNone(rec["verdict_changes"])
        self.assertTrue(rec["observer_called"])
        self.assertEqual(rec["tokens_in"], 100)
        self.assertEqual(rec["cost_usd_est"], 0.01)

    def test_state_write_failure_logged_but_pulse_continues(self):
        # state-write returns rc=1; main should still write heartbeat + return 0.
        run_calls = []
        def fake_run(cmd, **k):
            run_calls.append(cmd)
            if "state-write.py" in str(cmd):
                return (1, "", "atomic write failed")
            return (0, "", "")
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0,
            }):
            with mock.patch.object(self.mod, "purge_stale_awaiting"):
                with mock.patch.object(self.mod, "pick_open_todos",
                                       return_value={"bucket_b": []}):
                    with mock.patch.object(self.mod, "run", side_effect=fake_run):
                        with self.assertLogs("pulse", level="ERROR"):
                            rc = self._run_main([])
        self.assertEqual(rc, 0)
        # Heartbeat still written despite state-write failure.
        self.assertTrue((self._tmp / ".assistant/heartbeat.json").exists())


if __name__ == "__main__":
    unittest.main()
