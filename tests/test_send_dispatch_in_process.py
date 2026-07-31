"""Direct-import tests for cmux-send, merge-pr-dispatch, cmux-ws-numberer.

All three are CLI scripts that interact with subprocess — heavily mocked
here. We exercise the decision logic, parsing, and CLI shape without
ever calling cmux or gh for real."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
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
    return tmp


# ─── cmux-send ──────────────────────────────────────────────────────────────

class CmuxSendTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "cmux-send.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_utc_iso(self):
        s = self.mod.utc_iso()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_resolve_terminal_surface_finds_match(self):
        tree = {
            "windows": [{
                "workspaces": [{
                    "ref": "workspace:5", "title": "x",
                    "panes": [{"surfaces": [
                        {"type": "browser", "id": "B-1"},
                        {"type": "terminal", "id": "T-1", "ref": "surface:7", "tty": "ttys00"},
                    ]}],
                }],
            }],
        }
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout=json.dumps(tree))):
            sid, ref, tty, title = self.mod.resolve_terminal_surface("workspace:5")
        self.assertEqual(sid, "T-1")
        self.assertEqual(ref, "surface:7")
        self.assertEqual(tty, "ttys00")
        self.assertEqual(title, "x")

    def test_resolve_terminal_surface_returns_none_on_rc(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(self.mod.resolve_terminal_surface("workspace:1"),
                             (None, None, None, None))

    def test_resolve_terminal_surface_returns_none_on_exception(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               side_effect=Exception("boom")):
            self.assertEqual(self.mod.resolve_terminal_surface("workspace:1"),
                             (None, None, None, None))

    def test_resolve_terminal_surface_skips_non_terminal_surfaces(self):
        tree = {"windows": [{"workspaces": [{
            "ref": "workspace:5",
            "panes": [{"surfaces": [{"type": "browser", "id": "B"}]}],
        }]}]}
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout=json.dumps(tree))):
            sid, *_ = self.mod.resolve_terminal_surface("workspace:5")
        self.assertIsNone(sid)

    def test_find_transcript_for_ws_uses_world(self):
        Path(self.mod.WORLD).write_text(json.dumps({
            "live_sessions": [{"ws_ref": "workspace:1", "transcript_path": "/x.jsonl"}]
        }))
        self.assertEqual(self.mod.find_transcript_for_ws("workspace:1"), "/x.jsonl")

    def test_find_transcript_for_ws_returns_none_when_missing(self):
        self.assertIsNone(self.mod.find_transcript_for_ws("workspace:99"))

    def test_find_transcript_for_ws_handles_corrupt_world(self):
        Path(self.mod.WORLD).write_text("{ bad")
        self.assertIsNone(self.mod.find_transcript_for_ws("workspace:1"))

    def test_file_size_handles_missing_path(self):
        self.assertIsNone(self.mod.file_size(None))
        self.assertIsNone(self.mod.file_size("/no/such/file/path-xyz"))

    def test_file_size_returns_size(self):
        p = self._tmp / "f.txt"
        p.write_text("hello")
        self.assertEqual(self.mod.file_size(str(p)), 5)

    def test_append_log_appends_jsonl(self):
        self.mod.append_log({"a": 1})
        self.mod.append_log({"b": 2})
        lines = Path(self.mod.SENDS_LOG).read_text().splitlines()
        self.assertEqual(len(lines), 2)

    def test_rpc_handles_timeout(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10)):
            rc, body = self.mod.rpc("surface.send_text", {})
        self.assertEqual(rc, 124)
        self.assertEqual(body, {"error": "timeout"})

    def test_rpc_parses_json_body(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout='{"surface_id":"X"}',
                                                      stderr="")):
            rc, body = self.mod.rpc("send_text", {})
        self.assertEqual(rc, 0)
        self.assertEqual(body, {"surface_id": "X"})

    def test_rpc_falls_back_to_raw_text(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout="just text",
                                                      stderr="")):
            rc, body = self.mod.rpc("send_text", {})
        self.assertEqual(body, "just text")

    def _run_main(self, argv, exit_code=None):
        sys.argv = ["cmux-send.py"] + argv
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
        if exit_code is not None:
            self.assertEqual(cm.exception.code, exit_code)
        return cm.exception.code, out.getvalue()

    def test_main_exits_1_when_no_terminal_surface(self):
        with mock.patch.object(self.mod, "resolve_terminal_surface",
                               return_value=(None, None, None, None)):
            rc, out = self._run_main([
                "--ws", "workspace:1", "--text", "/cleanup", "--no-log"
            ], exit_code=1)
        self.assertIn("no_terminal_surface", out)

    def test_main_exits_2_when_send_text_fails(self):
        with mock.patch.object(self.mod, "resolve_terminal_surface",
                               return_value=("S-1", "surface:1", "ttys00", "title")):
            with mock.patch.object(self.mod, "rpc",
                                   return_value=(1, {"error": "rpc failed"})):
                rc, out = self._run_main([
                    "--ws", "workspace:1", "--text", "/cleanup", "--no-log"
                ], exit_code=2)
        self.assertIn("rpc_send_text_failed", out)

    def test_main_exits_2_when_send_key_fails(self):
        rpc_seq = [(0, {"surface_id": "S-1"}), (1, {"error": "key failed"})]
        with mock.patch.object(self.mod, "resolve_terminal_surface",
                               return_value=("S-1", "surface:1", "ttys00", "title")):
            with mock.patch.object(self.mod, "rpc",
                                   side_effect=rpc_seq):
                rc, out = self._run_main([
                    "--ws", "workspace:1", "--text", "/cleanup", "--enter", "--no-log"
                ], exit_code=2)
        self.assertIn("rpc_send_key_failed", out)

    def test_main_success_path(self):
        # Build a transcript file so size_before/after are real ints.
        tp = self._tmp / "t.jsonl"
        tp.write_text("hi\n")
        Path(self.mod.WORLD).write_text(json.dumps({
            "live_sessions": [{"ws_ref": "workspace:1", "transcript_path": str(tp)}]
        }))
        with mock.patch.object(self.mod, "resolve_terminal_surface",
                               return_value=("S-1", "surface:1", "ttys00", "title")):
            with mock.patch.object(self.mod, "rpc",
                                   return_value=(0, {"surface_id": "S-1"})):
                with mock.patch.object(self.mod.time, "sleep"):  # skip real sleep
                    rc, out = self._run_main([
                        "--ws", "workspace:1", "--text", "/cleanup",
                        "--enter", "--no-log", "--post-send-wait", "0"
                    ], exit_code=0)
        d = json.loads(out)
        self.assertEqual(d["outcome"], "sent")


# ─── cmux-ws-numberer ───────────────────────────────────────────────────────

class CmuxWsNumbererTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        # Need ~/.claude/logs/ for the logger.
        (self._tmp / ".claude/logs").mkdir(parents=True)
        self.mod = load_module(self._tmp, "cmux-ws-numberer.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_desired_title_appends_when_no_suffix(self):
        self.assertEqual(self.mod.desired_title("My Workspace", 7),
                         "My Workspace [7]")

    def test_desired_title_replaces_existing_suffix(self):
        self.assertEqual(self.mod.desired_title("My Workspace [3]", 7),
                         "My Workspace [7]")

    def test_desired_title_handles_trailing_whitespace(self):
        # Trailing whitespace is stripped before the suffix is appended.
        self.assertEqual(self.mod.desired_title("  Title  ", 1), "Title [1]")

    def test_list_workspaces_parses_output(self):
        # list_workspaces uses `cmux list-workspaces --json` → JSON payload,
        # and carries each workspace's custom_color (upper-cased, or None).
        import json as _json
        data = {"workspaces": [
            {"ref": "workspace:5", "title": "My Title", "custom_color": "#1565c0"},
            {"ref": "workspace:6", "title": "Second Title", "custom_color": None},
        ]}
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout=_json.dumps(data))):
            rows = self.mod.list_workspaces()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ref"], 5)
        self.assertEqual(rows[0]["title"], "My Title")
        self.assertEqual(rows[0]["color"], "#1565C0")
        self.assertEqual(rows[1]["title"], "Second Title")
        self.assertIsNone(rows[1]["color"])

    def test_list_workspaces_skips_non_matching_lines(self):
        # Entries whose "ref" field does not match "workspace:N" are skipped.
        import json as _json
        data = {"workspaces": [
            {"ref": "not-a-workspace", "title": "Bad"},
            {"ref": "workspace:1", "title": "Title"},
        ]}
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout=_json.dumps(data))):
            rows = self.mod.list_workspaces()
        # Only the entry with a valid workspace ref is returned.
        self.assertEqual(len(rows), 1)

    @staticmethod
    def _echo(ref: str) -> str:
        # workspace-action echoes the resolved ref on stdout; the daemon parses
        # it to defeat the silent fallback-to-selected. A faithful fake must
        # echo back the ref it was handed.
        return f"OK workspace={ref}"

    def _fake_run_factory(self, calls):
        # Records every command and returns a success whose stdout echoes the
        # --workspace ref that was passed, mirroring real workspace-action.
        def fake_run(cmd, **k):
            calls.append(cmd)
            ref = ""
            if "--workspace" in cmd:
                ref = cmd[cmd.index("--workspace") + 1]
            return mock.Mock(returncode=0, stdout=self._echo(ref), stderr="")
        return fake_run

    @staticmethod
    def _renames(calls):
        # New CLI shape: ["cmux", "workspace-action", "--action", "rename", ...]
        return [c for c in calls
                if "workspace-action" in c and "rename" in c]

    @staticmethod
    def _colorings(calls):
        return [c for c in calls
                if "workspace-action" in c and "set-color" in c]

    def test_ensure_numbered_no_op_when_already_numbered(self):
        # reconcile() skips a workspace that already has the correct [N] suffix
        # AND already has a color — no rename, no set-color.
        with mock.patch.object(self.mod, "list_workspaces", return_value=[
            {"ref": 7, "title": "Title [7]", "color": "#1565C0"},
        ]):
            with mock.patch.object(self.mod.subprocess, "run") as run_mock:
                self.mod.reconcile()
        run_mock.assert_not_called()

    def test_ensure_numbered_renames_when_mismatched(self):
        # reconcile() renames a workspace that lacks or has a wrong suffix.
        with mock.patch.object(self.mod, "list_workspaces", return_value=[
            {"ref": 7, "title": "Title", "color": "#1565C0"},
        ]):
            calls = []
            with mock.patch.object(self.mod.subprocess, "run",
                                   side_effect=self._fake_run_factory(calls)):
                self.mod.reconcile()
        rename_call = next(iter(self._renames(calls)), None)
        self.assertIsNotNone(rename_call)
        # Last positional should be the new title "Title [7]".
        self.assertEqual(rename_call[-1], "Title [7]")

    def test_ensure_numbered_logs_rename_failure(self):
        # reconcile() does not raise when the rename command fails.
        with mock.patch.object(self.mod, "list_workspaces", return_value=[
            {"ref": 7, "title": "Title", "color": "#1565C0"},
        ]):
            with mock.patch.object(self.mod.subprocess, "run",
                                   return_value=mock.Mock(returncode=1, stdout="",
                                                          stderr="rename failed")):
                # Should not raise.
                self.mod.reconcile()

    def test_ensure_numbered_warns_when_uuid_missing(self):
        # reconcile() with an empty list is a no-op (nothing to rename).
        with mock.patch.object(self.mod, "list_workspaces", return_value=[]):
            self.mod.reconcile()  # no crash

    def test_backfill_renames_each_unnumbered(self):
        # reconcile() renames all workspaces that lack the correct suffix.
        ws_list = [
            {"ref": 7, "title": "Already [7]", "color": "#1565C0"},   # skip
            {"ref": 8, "title": "Plain", "color": "#922B21"},          # rename
            {"ref": 9, "title": "Wrong [99]", "color": "#196F3D"},     # rename
        ]
        with mock.patch.object(self.mod, "list_workspaces", return_value=ws_list):
            calls = []
            with mock.patch.object(self.mod.subprocess, "run",
                                   side_effect=self._fake_run_factory(calls)):
                self.mod.reconcile()
        # Expect 2 rename calls (for refs 8 and 9); all three already colored.
        self.assertEqual(len(self._renames(calls)), 2)
        self.assertEqual(len(self._colorings(calls)), 0)

    def test_backfill_colors_each_uncolored(self):
        # reconcile() assigns a color to every workspace whose color is None,
        # and leaves already-colored workspaces untouched.
        ws_list = [
            {"ref": 7, "title": "A [7]", "color": "#1565C0"},   # has color → skip
            {"ref": 8, "title": "B [8]", "color": None},         # color it
            {"ref": 9, "title": "C [9]", "color": None},         # color it
        ]
        with mock.patch.object(self.mod, "list_workspaces", return_value=ws_list):
            calls = []
            with mock.patch.object(self.mod.subprocess, "run",
                                   side_effect=self._fake_run_factory(calls)):
                self.mod.reconcile()
        colorings = self._colorings(calls)
        self.assertEqual(len(colorings), 2)
        # Each set-color targets a distinct workspace ref (8 and 9).
        colored_refs = {c[c.index("--workspace") + 1] for c in colorings}
        self.assertEqual(colored_refs, {"workspace:8", "workspace:9"})

    def test_action_skipped_when_ref_resolves_to_other_workspace(self):
        # If workspace-action echoes a DIFFERENT ref than targeted (cmux fell
        # back to the selected workspace because the target closed mid-poll),
        # the daemon must treat it as a no-op, not a success.
        with mock.patch.object(self.mod, "list_workspaces", return_value=[
            {"ref": 8, "title": "Plain", "color": "#1565C0"},
        ]):
            def fallback_run(cmd, **k):
                # Always echo the SELECTED workspace, not the target.
                return mock.Mock(returncode=0, stdout="OK workspace=workspace:1",
                                 stderr="")
            with mock.patch.object(self.mod.subprocess, "run",
                                   side_effect=fallback_run):
                # Must not raise; the mismatch is logged and skipped.
                ok = self.mod._run_action("rename", "workspace:8",
                                          ["--title", "Plain [8]"])
        self.assertFalse(ok)


# ─── merge-pr-dispatch ──────────────────────────────────────────────────────

class MergePrDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "merge-pr-dispatch.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_test_path_re_matches_expected_paths(self):
        good = [
            "e2e/foo.spec.ts",
            "src/foo/__tests__/bar.test.ts",
            "src/foo/bar.spec.tsx",
            "src/foo/bar.test.js",
            "fixtures/clip.json",
            "page-objects/timeline.po.ts",
        ]
        for p in good:
            self.assertIsNotNone(self.mod.TEST_PATH_RE.match(p), p)
        bad = ["src/foo/bar.ts", "docs/README.md", "package.json"]
        for p in bad:
            self.assertIsNone(self.mod.TEST_PATH_RE.match(p), p)

    def test_step0_already_merged(self):
        with mock.patch.object(self.mod, "gh_pr_view",
                               return_value={"state": "MERGED",
                                              "mergedAt": "2026-01-01",
                                              "files": [], "title": "", "body": ""}):
            ok, reason, _ = self.mod.step0_safety_gate(100, refactor_attested=False)
        self.assertFalse(ok)
        self.assertEqual(reason, "already_merged")

    def test_step0_test_only_passes(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "state": "OPEN", "mergedAt": None,
                "files": [{"path": "e2e/foo.spec.ts"}, {"path": "fixtures/x.json"}],
                "title": "test: hardening",
                "body": "",
            }):
            ok, reason, _ = self.mod.step0_safety_gate(100, refactor_attested=False)
        self.assertTrue(ok)
        self.assertEqual(reason, "test_only")

    def test_step0_refactor_attested_passes_with_signal(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "state": "OPEN", "mergedAt": None,
                "files": [{"path": "src/foo/bar.ts"}],
                "title": "rename(foo): rename A to B",
                "body": "no behavior change",
            }):
            ok, reason, _ = self.mod.step0_safety_gate(100, refactor_attested=True)
        self.assertTrue(ok)
        self.assertEqual(reason, "refactor_attested")

    def test_step0_refactor_attested_fails_without_signal(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "state": "OPEN", "mergedAt": None,
                "files": [{"path": "src/foo/bar.ts"}],
                "title": "feat: new feature",
                "body": "ship it",
            }):
            ok, reason, _ = self.mod.step0_safety_gate(100, refactor_attested=True)
        self.assertFalse(ok)
        self.assertEqual(reason, "refactor_attested_but_no_signal")

    def test_step0_not_auto_mergeable_default(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "state": "OPEN", "mergedAt": None,
                "files": [{"path": "src/foo/bar.ts"}],
                "title": "feat: x", "body": "",
            }):
            ok, reason, _ = self.mod.step0_safety_gate(100, refactor_attested=False)
        self.assertFalse(ok)
        self.assertEqual(reason, "not_auto_mergeable")

    def test_step1_routes_to_merge_when_ready_when_all_green(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "statusCheckRollup": [
                    {"name": "ci", "conclusion": "SUCCESS"},
                    {"name": "lint", "conclusion": "SKIPPED"},
                ],
                "reviewDecision": "APPROVED",
            }):
            skill, reason, _ = self.mod.step1_ci_route(100)
        self.assertEqual(skill, "merge-when-ready")
        self.assertEqual(reason, "ci_all_green")

    def test_step1_routes_to_monitor_when_failing(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "statusCheckRollup": [
                    {"name": "ci", "conclusion": "FAILURE"},
                ],
                "reviewDecision": None,
            }):
            skill, reason, ev = self.mod.step1_ci_route(100)
        self.assertEqual(skill, "monitor-ffp-ci")
        self.assertEqual(reason, "ci_not_all_green")
        self.assertEqual(len(ev["failing"]), 1)

    def test_step1_routes_to_monitor_when_pending(self):
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "statusCheckRollup": [
                    {"name": "ci", "conclusion": "PENDING"},
                ],
                "reviewDecision": None,
            }):
            skill, _, ev = self.mod.step1_ci_route(100)
        self.assertEqual(skill, "monitor-ffp-ci")
        self.assertEqual(len(ev["pending"]), 1)

    def test_step1_handles_status_context_state_field(self):
        # Some checks use 'state' instead of 'conclusion'.
        with mock.patch.object(self.mod, "gh_pr_view", return_value={
                "statusCheckRollup": [
                    {"context": "external", "state": "FAILURE"},
                ],
                "reviewDecision": None,
            }):
            skill, _, _ = self.mod.step1_ci_route(100)
        self.assertEqual(skill, "monitor-ffp-ci")

    def test_step2_returns_false_when_send_bad_output(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout="not json",
                                                      stderr="")):
            ok, msg = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertFalse(ok)
        self.assertIn("cmux_send_bad_output", msg)

    def test_step2_returns_false_when_send_exit_nonzero(self):
        send_record = {"outcome": "no_terminal_surface", "rpc_send_text": {"body": ""}}
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=1,
                                                      stdout=json.dumps(send_record),
                                                      stderr="")):
            ok, msg = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertFalse(ok)
        self.assertIn("cmux_send_exit", msg)

    def test_step2_returns_false_when_delta_zero(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 0,
                       "target_surface_id": "abc", "target_ws_title": "t"}
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout=json.dumps(send_record),
                                                      stderr="")):
            ok, msg = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertFalse(ok)
        self.assertIn("transcript_delta_zero", msg)

    def test_step2_verifies_via_skill_body_match(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 1024}
        tail = {"last_user": {"text": "monitor-ffp-ci body... ARGUMENTS: 100"}}
        run_seq = [
            mock.Mock(returncode=0, stdout=json.dumps(send_record), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps(tail), stderr=""),
        ]
        with mock.patch.object(self.mod.subprocess, "run", side_effect=run_seq):
            ok, observed = self.mod.step2_send_and_verify(
                "workspace:1", "/monitor-ffp-ci 100")
        self.assertTrue(ok)

    def test_step2_verifies_via_bare_match(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 1024}
        tail = {"last_user": {"text": "/foo 100"}}
        run_seq = [
            mock.Mock(returncode=0, stdout=json.dumps(send_record), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps(tail), stderr=""),
        ]
        with mock.patch.object(self.mod.subprocess, "run", side_effect=run_seq):
            ok, _ = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertTrue(ok)

    def test_step2_verifies_via_wrapper_match(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 1024}
        tail = {"last_user": {
            "text": "<command-name>/foo</command-name> 100"
        }}
        run_seq = [
            mock.Mock(returncode=0, stdout=json.dumps(send_record), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps(tail), stderr=""),
        ]
        with mock.patch.object(self.mod.subprocess, "run", side_effect=run_seq):
            ok, _ = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertTrue(ok)

    def test_step2_returns_false_on_no_match(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 1024}
        tail = {"last_user": {"text": "completely unrelated text"}}
        run_seq = [
            mock.Mock(returncode=0, stdout=json.dumps(send_record), stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps(tail), stderr=""),
        ]
        with mock.patch.object(self.mod.subprocess, "run", side_effect=run_seq):
            ok, _ = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertFalse(ok)

    def test_step2_returns_false_on_tail_rc(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 1024}
        run_seq = [
            mock.Mock(returncode=0, stdout=json.dumps(send_record), stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="tail failed"),
        ]
        with mock.patch.object(self.mod.subprocess, "run", side_effect=run_seq):
            ok, msg = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertFalse(ok)
        self.assertIn("transcript_tail_exit", msg)

    def test_step2_returns_false_on_tail_bad_json(self):
        send_record = {"outcome": "sent", "transcript_size_delta": 1024}
        run_seq = [
            mock.Mock(returncode=0, stdout=json.dumps(send_record), stderr=""),
            mock.Mock(returncode=0, stdout="not json", stderr=""),
        ]
        with mock.patch.object(self.mod.subprocess, "run", side_effect=run_seq):
            ok, msg = self.mod.step2_send_and_verify("workspace:1", "/foo 100")
        self.assertFalse(ok)
        self.assertIn("transcript_tail_bad_json", msg)

    def _run_main(self, argv):
        sys.argv = ["merge-pr-dispatch.py"] + argv
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
        return cm.exception.code, out.getvalue()

    def test_main_refused_when_safety_gate_fails(self):
        with mock.patch.object(self.mod, "step0_safety_gate",
                               return_value=(False, "not_auto_mergeable", {})):
            rc, out = self._run_main([
                "--ws", "workspace:1", "--pr", "100"
            ])
        self.assertEqual(rc, 1)
        d = json.loads(out)
        self.assertEqual(d["outcome"], "refused")
        self.assertIn("awaiting_card", d)

    def test_main_send_unverified_path(self):
        with mock.patch.object(self.mod, "step0_safety_gate",
                               return_value=(True, "test_only", {})):
            with mock.patch.object(self.mod, "gh_pr_view",
                                   return_value={"files": []}):
                with mock.patch.object(self.mod, "step_freeze_retarget",
                                       return_value=(False, "not_blocked", {})):
                    with mock.patch.object(self.mod, "step1_ci_route",
                                           return_value=("merge-when-ready", "ci_all_green", {})):
                        with mock.patch.object(self.mod, "step2_send_and_verify",
                                               return_value=(False, "did not match")):
                            rc, out = self._run_main([
                                "--ws", "workspace:1", "--pr", "100"
                            ])
        self.assertEqual(rc, 2)
        d = json.loads(out)
        self.assertEqual(d["outcome"], "send_unverified")

    def test_main_submitted_path(self):
        with mock.patch.object(self.mod, "step0_safety_gate",
                               return_value=(True, "test_only", {})):
            with mock.patch.object(self.mod, "gh_pr_view",
                                   return_value={"files": []}):
                with mock.patch.object(self.mod, "step_freeze_retarget",
                                       return_value=(False, "not_blocked", {})):
                    with mock.patch.object(self.mod, "step1_ci_route",
                                           return_value=("merge-when-ready", "ci_all_green", {})):
                        with mock.patch.object(self.mod, "step2_send_and_verify",
                                               return_value=(True, "/merge-when-ready 100")):
                            rc, out = self._run_main([
                                "--ws", "workspace:1", "--pr", "100"
                            ])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["outcome"], "submitted")


if __name__ == "__main__":
    unittest.main()
