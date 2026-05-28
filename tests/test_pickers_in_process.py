"""Direct-import tests for bin/pick-ws-batch.py, bin/pick-open-todos.py,
and bin/purge-stale-awaiting.py.

Existing test_purge_stale_awaiting.py runs via subprocess and the picker
scripts have no in-process tests. This file imports each module directly
and exercises every branch.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import textwrap
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


# ─── pick-ws-batch ──────────────────────────────────────────────────────────

class PickWsBatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "pick-ws-batch.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _run_main_with_cmux_response(self, ws_list):
        """Patch subprocess.check_output to return ws_list as cmux output."""
        out_capture = io.StringIO()
        with mock.patch.object(self.mod.subprocess, "check_output",
                               return_value=json.dumps(ws_list)):
            with mock.patch("sys.stdout", out_capture):
                self.mod.main()
        return json.loads(out_capture.getvalue())

    def test_load_back_off_refs_returns_empty_when_missing(self):
        # back-off.json doesn't exist in tempdir.
        self.assertEqual(self.mod.load_back_off_refs(), {})

    def test_load_back_off_refs_returns_empty_on_corrupt_json(self):
        Path(self.mod.BACK_OFF_PATH).write_text("{ not json")
        self.assertEqual(self.mod.load_back_off_refs(), {})

    def test_load_back_off_refs_skips_entries_without_ws_ref(self):
        Path(self.mod.BACK_OFF_PATH).write_text(json.dumps({
            "workspaces": [
                {"ws_ref": "workspace:1", "reason": "loop"},
                {"reason": "missing ref"},  # should be filtered
                {"ws_ref": "workspace:2", "reason": "manual"},
            ]
        }))
        d = self.mod.load_back_off_refs()
        self.assertEqual(set(d.keys()), {"workspace:1", "workspace:2"})

    def test_main_basic_ranking(self):
        out = self._run_main_with_cmux_response([
            {"ref": "workspace:1", "title": "a", "current_directory": "/a"},
            {"ref": "workspace:2", "title": "b", "current_directory": "/b"},
        ])
        self.assertEqual(out["total_ws"], 2)
        # Both have no summary file; ranked by ts=0, both go in to_reclassify.
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertIn("workspace:1", refs)
        self.assertIn("workspace:2", refs)
        self.assertEqual(out["backed_off"], [])

    def test_main_excludes_backed_off(self):
        Path(self.mod.BACK_OFF_PATH).write_text(json.dumps({
            "workspaces": [{"ws_ref": "workspace:9", "reason": "loop"}]
        }))
        out = self._run_main_with_cmux_response([
            {"ref": "workspace:1", "title": "a"},
            {"ref": "workspace:9", "title": "b"},
        ])
        self.assertEqual([b["ref"] for b in out["backed_off"]], ["workspace:9"])
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertNotIn("workspace:9", refs)
        self.assertIn("workspace:1", refs)

    def test_main_skips_entries_without_ref(self):
        out = self._run_main_with_cmux_response([
            {"ref": "workspace:1", "title": "a"},
            {"title": "no-ref"},  # filtered
        ])
        self.assertEqual(out["total_ws"], 1)

    def test_main_handles_dict_payload_shape(self):
        # cmux can return either a list directly or {"workspaces": [...]}.
        # Test the dict-shaped variant.
        out_capture = io.StringIO()
        with mock.patch.object(self.mod.subprocess, "check_output",
                               return_value=json.dumps({"workspaces": [
                                   {"ref": "workspace:1", "title": "a"}]})):
            with mock.patch("sys.stdout", out_capture):
                self.mod.main()
        out = json.loads(out_capture.getvalue())
        self.assertEqual(out["total_ws"], 1)

    def test_main_lru_oldest_first(self):
        # Two workspaces, one with an older summary timestamp.
        summ_dir = Path(self.mod.SUMM_DIR)
        summ_dir.mkdir(parents=True, exist_ok=True)
        (summ_dir / "workspace_1.json").write_text(json.dumps({"last_updated_ts": 100}))
        (summ_dir / "workspace_2.json").write_text(json.dumps({"last_updated_ts": 200}))
        out = self._run_main_with_cmux_response([
            {"ref": "workspace:1", "title": "a"},
            {"ref": "workspace:2", "title": "b"},
        ])
        self.assertEqual(out["to_reclassify"][0]["ref"], "workspace:1")  # older first

    def test_main_handles_corrupt_summary(self):
        summ_dir = Path(self.mod.SUMM_DIR)
        summ_dir.mkdir(parents=True, exist_ok=True)
        (summ_dir / "workspace_1.json").write_text("{ not json")
        # Should not crash; treats ts=0.
        out = self._run_main_with_cmux_response([
            {"ref": "workspace:1", "title": "a"}
        ])
        self.assertEqual(out["total_ws"], 1)


# ─── pick-open-todos ────────────────────────────────────────────────────────

class PickOpenTodosTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "pick-open-todos.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _write_todo(self, items):
        Path(self.mod.TODO_PATH).write_text(json.dumps({"items": items}))

    def _run_main(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            try:
                self.mod.main()
            except SystemExit:
                pass
        return out.getvalue()

    def test_missing_todo_file_exits_with_error(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("todo file missing", out.getvalue())

    def test_buckets_dispatch_candidate(self):
        self._write_todo([
            {"id": "td-1", "status": "open", "autoDispatch": True,
             "title": "ready to spawn", "priority": "P1"},
        ])
        with mock.patch.object(self.mod, "live_workspaces", return_value=set()):
            out = json.loads(self._run_main())
        self.assertEqual(len(out["bucket_b"]), 1)
        self.assertEqual(out["bucket_b"][0]["id"], "td-1")

    def test_buckets_in_flight_correctly(self):
        self._write_todo([
            {"id": "td-1", "status": "open", "autoDispatch": True,
             "dispatched_ws": "workspace:5", "dispatched_at": "2026-01-01"},
        ])
        with mock.patch.object(self.mod, "live_workspaces",
                               return_value={"workspace:5"}):
            out = json.loads(self._run_main())
        self.assertEqual(len(out["skipped_in_flight"]), 1)

    def test_buckets_orphaned_dispatch(self):
        # Workspace dispatched but no longer alive — bucket A.
        self._write_todo([
            {"id": "td-1", "status": "open", "autoDispatch": True,
             "dispatched_ws": "workspace:99", "dispatched_at": "2026-01-01"},
        ])
        with mock.patch.object(self.mod, "live_workspaces",
                               return_value={"workspace:1"}):
            out = json.loads(self._run_main())
        self.assertEqual(len(out["bucket_a"]), 1)

    def test_buckets_undecided_to_c(self):
        self._write_todo([
            {"id": "td-1", "status": "open", "autoDispatch": None,
             "title": "decide later", "priority": "P2"},
        ])
        with mock.patch.object(self.mod, "live_workspaces", return_value=set()):
            out = json.loads(self._run_main())
        self.assertEqual(len(out["bucket_c"]), 1)
        self.assertEqual(out["bucket_c"][0]["id"], "td-1")

    def test_skipped_manual(self):
        self._write_todo([
            {"id": "td-1", "status": "open", "autoDispatch": False},
        ])
        with mock.patch.object(self.mod, "live_workspaces", return_value=set()):
            out = json.loads(self._run_main())
        self.assertEqual(len(out["skipped_manual"]), 1)

    def test_skips_non_open(self):
        self._write_todo([
            {"id": "td-1", "status": "done", "autoDispatch": True},
            {"id": "td-2", "status": "deferred", "autoDispatch": True},
        ])
        with mock.patch.object(self.mod, "live_workspaces", return_value=set()):
            out = json.loads(self._run_main())
        self.assertEqual(out["totals"]["open"], 0)

    def test_supports_camelcase_dispatched_fields(self):
        # The mechanic doesn't care if it's dispatchedAt vs dispatched_at.
        self._write_todo([
            {"id": "td-1", "status": "open", "autoDispatch": True,
             "dispatchedAt": "2026", "dispatchedWs": "workspace:1"},
        ])
        with mock.patch.object(self.mod, "live_workspaces", return_value=set()):
            out = json.loads(self._run_main())
        # dispatched_ws not in live → bucket_a.
        self.assertEqual(len(out["bucket_a"]), 1)

    def test_live_workspaces_prefers_world_json(self):
        Path(self.mod.WORLD_PATH).write_text(json.dumps({
            "live_sessions": [{"ws_ref": "workspace:7"}]
        }))
        result = self.mod.live_workspaces()
        self.assertEqual(result, {"workspace:7"})

    def test_live_workspaces_falls_back_to_cmux(self):
        # No world.json. Mock cmux subprocess.
        with mock.patch.object(self.mod.subprocess, "check_output",
                               return_value=json.dumps([{"ref": "workspace:8"}])):
            result = self.mod.live_workspaces()
        self.assertEqual(result, {"workspace:8"})

    def test_live_workspaces_returns_empty_on_total_failure(self):
        with mock.patch.object(self.mod.subprocess, "check_output",
                               side_effect=Exception("boom")):
            result = self.mod.live_workspaces()
        self.assertEqual(result, set())

    def test_live_workspaces_dict_payload_fallback(self):
        # cmux returns dict shape from subprocess fallback.
        with mock.patch.object(self.mod.subprocess, "check_output",
                               return_value=json.dumps(
                                   {"workspaces": [{"ref": "workspace:9"}]})):
            result = self.mod.live_workspaces()
        self.assertEqual(result, {"workspace:9"})


# ─── purge-stale-awaiting ──────────────────────────────────────────────────

class PurgeStaleAwaitingTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp, "purge-stale-awaiting.py")

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _set_state(self, cards):
        self.mod.STATE_PATH.write_text(json.dumps({
            "awaiting_input": cards,
        }))

    def test_log_writes_line(self):
        self.mod.log("hello")
        self.assertIn("hello", self.mod.LOG_PATH.read_text())

    def test_get_open_workspaces_walks_tree(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=json.dumps({
                                   "windows": [{"workspaces": [
                                       {"ref": "workspace:1"},
                                       {"workspace_ref": "workspace:2"},
                                   ]}]
                               }))):
            result = self.mod.get_open_workspaces()
        self.assertEqual(result, {"workspace:1", "workspace:2"})

    def test_get_open_workspaces_returns_empty_on_rc(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(self.mod.get_open_workspaces(), set())

    def test_get_open_workspaces_returns_empty_on_exception(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               side_effect=Exception("boom")):
            self.assertEqual(self.mod.get_open_workspaces(), set())

    def test_get_open_workspaces_via_text_fallback(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout="window window:1\nworkspace workspace:5 'foo'\n")):
            self.assertEqual(self.mod.get_open_workspaces_via_text(), {"workspace:5"})

    def test_get_open_workspaces_via_text_returns_empty_on_rc(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(self.mod.get_open_workspaces_via_text(), set())

    def test_load_todos_returns_dict_keyed_by_id(self):
        self.mod.TODO_PATH.write_text(json.dumps({"items": [
            {"id": "td-1", "status": "open"},
            {"id": "td-2", "status": "done"},
        ]}))
        d = self.mod.load_todos()
        self.assertEqual(set(d.keys()), {"td-1", "td-2"})

    def test_load_todos_handles_corrupt_file(self):
        self.mod.TODO_PATH.write_text("{ not json")
        self.assertEqual(self.mod.load_todos(), {})

    def test_load_todos_handles_list_shape(self):
        # Some older formats have items at the top level.
        self.mod.TODO_PATH.write_text(json.dumps([
            {"id": "td-1"}, {"id": "td-2"}
        ]))
        d = self.mod.load_todos()
        self.assertEqual(set(d.keys()), {"td-1", "td-2"})

    def test_gh_pr_state_returns_state(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="MERGED\n")):
            self.assertEqual(self.mod.gh_pr_state("100"), "MERGED")

    def test_gh_pr_state_returns_none_on_rc(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(self.mod.gh_pr_state("100"))

    def test_gh_pr_state_returns_none_on_exception(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               side_effect=Exception("boom")):
            self.assertIsNone(self.mod.gh_pr_state("100"))

    def test_card_should_drop_ws_closed(self):
        card = {"key": "ws-stuck:workspace:5", "detail": "..."}
        result = self.mod.card_should_drop(card, {"workspace:1"}, {})
        self.assertIn("workspace(s) closed", result)

    def test_card_should_drop_keeps_when_ws_still_open(self):
        card = {"key": "ws-stuck:workspace:5"}
        result = self.mod.card_should_drop(card, {"workspace:5"}, {})
        self.assertIsNone(result)

    def test_card_should_drop_pr_merged(self):
        card = {"key": "pr-100", "detail": ""}
        with mock.patch.object(self.mod, "gh_pr_state", return_value="MERGED"):
            result = self.mod.card_should_drop(card, {"workspace:1"}, {})
        self.assertIn("MERGED", result)

    def test_card_should_drop_pr_closed(self):
        card = {"detail": "PR #99 needs review", "key": "x"}
        with mock.patch.object(self.mod, "gh_pr_state", return_value="CLOSED"):
            result = self.mod.card_should_drop(card, {"workspace:1"}, {})
        self.assertIn("CLOSED", result)

    def test_card_should_drop_td_done(self):
        card = {"key": "td-stuck:td-007", "detail": ""}
        result = self.mod.card_should_drop(
            card, {"workspace:1"}, {"td-007": {"status": "done"}}
        )
        self.assertIn("done/deferred", result)

    def test_card_should_drop_td_keeps_when_unknown(self):
        card = {"key": "td-stuck:td-007"}
        # td-007 not in todos at all.
        self.assertIsNone(
            self.mod.card_should_drop(card, {"workspace:1"}, {})
        )

    def test_card_should_drop_autodispatch_unset_resolved(self):
        card = {"key": "autodispatch-unset:td-007"}
        result = self.mod.card_should_drop(
            card, {"workspace:1"},
            {"td-007": {"autoDispatch": True}},
        )
        self.assertIn("autoDispatch set", result)

    def test_card_should_drop_returns_none_when_nothing_matches(self):
        card = {"key": "nothing-special", "detail": "no signals"}
        self.assertIsNone(
            self.mod.card_should_drop(card, {"workspace:1"}, {})
        )

    def test_main_no_state_file_exits_quietly(self):
        # No state file at all → main returns silently.
        if self.mod.STATE_PATH.exists():
            self.mod.STATE_PATH.unlink()
        sys.argv = ["purge.py"]
        self.mod.main()  # no crash, no output

    def test_main_corrupt_state_logs_and_returns(self):
        self.mod.STATE_PATH.write_text("{ not json")
        sys.argv = ["purge.py"]
        self.mod.main()
        self.assertIn("unreadable", self.mod.LOG_PATH.read_text())

    def test_main_skips_when_no_cards(self):
        self._set_state([])
        sys.argv = ["purge.py"]
        self.mod.main()  # silent

    def test_main_skips_when_cmux_down(self):
        self._set_state([{"key": "x"}])
        sys.argv = ["purge.py"]
        with mock.patch.object(self.mod, "get_open_workspaces", return_value=set()):
            with mock.patch.object(self.mod, "get_open_workspaces_via_text", return_value=set()):
                self.mod.main()
        self.assertIn("cmux may be down", self.mod.LOG_PATH.read_text())

    def test_main_drops_card_and_writes_state(self):
        self._set_state([
            {"key": "ws-stuck:workspace:99"},
            {"key": "keep-me"},
        ])
        sys.argv = ["purge.py"]
        with mock.patch.object(self.mod, "get_open_workspaces",
                               return_value={"workspace:1"}):
            self.mod.main()
        d = json.loads(self.mod.STATE_PATH.read_text())
        keys = [c.get("key") for c in d["awaiting_input"]]
        self.assertEqual(keys, ["keep-me"])
        log_text = self.mod.LOG_PATH.read_text()
        self.assertIn("DROP ws-stuck:workspace:99", log_text)

    def test_main_dry_run_does_not_modify_state(self):
        self._set_state([{"key": "ws-stuck:workspace:99"}])
        sys.argv = ["purge.py", "--dry-run"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with mock.patch.object(self.mod, "get_open_workspaces",
                                   return_value={"workspace:1"}):
                self.mod.main()
        # State unchanged
        d = json.loads(self.mod.STATE_PATH.read_text())
        self.assertEqual(len(d["awaiting_input"]), 1)
        self.assertIn("would drop", out.getvalue())

    def test_main_does_nothing_when_no_cards_match(self):
        self._set_state([{"key": "keep"}, {"key": "also-keep"}])
        sys.argv = ["purge.py"]
        with mock.patch.object(self.mod, "get_open_workspaces",
                               return_value={"workspace:1"}):
            self.mod.main()
        d = json.loads(self.mod.STATE_PATH.read_text())
        self.assertEqual(len(d["awaiting_input"]), 2)


if __name__ == "__main__":
    unittest.main()
