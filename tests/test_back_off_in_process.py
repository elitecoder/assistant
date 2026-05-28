"""Direct-import tests for bin/back-off.py.

The existing test_back_off.py runs the CLI via subprocess so coverage
shows 0% even though the code paths are exercised. This test re-loads
the module with HOME pointed at a tempdir so coverage measures every
branch.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/back-off.py"


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("back_off_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant").mkdir(parents=True)
    return tmp


class LoadSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_load_returns_default_when_file_missing(self):
        self.assertEqual(self.mod.load(), {"workspaces": []})

    def test_load_returns_default_on_corrupt_json(self):
        self.mod.PATH.write_text("{ not json")
        self.assertEqual(self.mod.load(), {"workspaces": []})

    def test_save_writes_atomically_and_load_round_trips(self):
        d = {"workspaces": [{"ws_ref": "workspace:1", "reason": "x"}]}
        self.mod.save(d)
        self.assertEqual(self.mod.load(), d)
        # No .tmp leak.
        self.assertFalse(self.mod.PATH.with_suffix(".json.tmp").exists())


class CommandTests(unittest.TestCase):
    """Each cmd_* function takes argparse Namespace; build the namespace
    by hand and dispatch directly. Captures stdout/stderr."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _capture(self, fn, ns) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = fn(ns)
        return rc, out.getvalue(), err.getvalue()

    def test_add_rejects_non_workspace_ref(self):
        ns = argparse.Namespace(ws_ref="bogus", reason="r")
        rc, _, err = self._capture(self.mod.cmd_add, ns)
        self.assertEqual(rc, 2)
        self.assertIn("must look like 'workspace:N'", err)

    def test_add_creates_entry(self):
        ns = argparse.Namespace(ws_ref="workspace:7", reason="loop")
        rc, out, _ = self._capture(self.mod.cmd_add, ns)
        self.assertEqual(rc, 0)
        self.assertIn("added workspace:7", out)
        d = self.mod.load()
        self.assertEqual(len(d["workspaces"]), 1)
        self.assertEqual(d["workspaces"][0]["ws_ref"], "workspace:7")

    def test_add_idempotent_updates_reason(self):
        self._capture(self.mod.cmd_add,
                       argparse.Namespace(ws_ref="workspace:7", reason="first"))
        rc, out, _ = self._capture(self.mod.cmd_add,
                                    argparse.Namespace(ws_ref="workspace:7", reason="second"))
        self.assertEqual(rc, 0)
        self.assertIn("updated workspace:7", out)
        d = self.mod.load()
        self.assertEqual(len(d["workspaces"]), 1)
        self.assertEqual(d["workspaces"][0]["reason"], "second")

    def test_remove_drops_entry(self):
        self._capture(self.mod.cmd_add,
                       argparse.Namespace(ws_ref="workspace:7", reason="r"))
        self._capture(self.mod.cmd_add,
                       argparse.Namespace(ws_ref="workspace:8", reason="r2"))
        rc, out, _ = self._capture(self.mod.cmd_remove,
                                    argparse.Namespace(ws_ref="workspace:7"))
        self.assertEqual(rc, 0)
        self.assertIn("removed workspace:7", out)
        d = self.mod.load()
        self.assertEqual([w["ws_ref"] for w in d["workspaces"]], ["workspace:8"])

    def test_remove_nonexistent_returns_1(self):
        rc, out, _ = self._capture(self.mod.cmd_remove,
                                    argparse.Namespace(ws_ref="workspace:99"))
        self.assertEqual(rc, 1)
        self.assertIn("not on back-off list", out)

    def test_list_empty(self):
        rc, out, _ = self._capture(self.mod.cmd_list, argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertIn("back-off list is empty", out)

    def test_list_renders_each_entry(self):
        self._capture(self.mod.cmd_add,
                       argparse.Namespace(ws_ref="workspace:7", reason="loop"))
        self._capture(self.mod.cmd_add,
                       argparse.Namespace(ws_ref="workspace:8", reason="user paused"))
        rc, out, _ = self._capture(self.mod.cmd_list, argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertIn("workspace:7", out)
        self.assertIn("workspace:8", out)
        self.assertIn("loop", out)
        self.assertIn("user paused", out)

    def test_list_handles_missing_added_ts(self):
        # An entry without added_ts should render age=-1.
        self.mod.save({"workspaces": [{"ws_ref": "workspace:7", "reason": "r"}]})
        rc, out, _ = self._capture(self.mod.cmd_list, argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertIn("added=-1s", out)


class MainEntrypointTests(unittest.TestCase):
    """main() wires argparse → cmd_* via set_defaults(func=). Hit each
    subcommand to confirm dispatch."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _run_main(self, argv):
        sys.argv = ["back-off.py"] + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = self.mod.main()
        return rc, out.getvalue(), err.getvalue()

    def test_main_add(self):
        rc, out, _ = self._run_main(["add", "workspace:1", "reason"])
        self.assertEqual(rc, 0)
        self.assertIn("added", out)

    def test_main_remove(self):
        self._run_main(["add", "workspace:1", "reason"])
        rc, out, _ = self._run_main(["remove", "workspace:1"])
        self.assertEqual(rc, 0)

    def test_main_list(self):
        rc, _, _ = self._run_main(["list"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
