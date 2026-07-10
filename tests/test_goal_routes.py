"""Tests for the todo-server /goal/* routes (Keel M4). Imports bin/todo-server.py
by path (stdlib-only, so it loads under both python3.9 and python3.12 — unlike
test_todo_server which pulls in pytest) and drives the route HANDLER functions
directly: goal_list / goal_add / goal_update / goal_rerank. The routes are the
HUMAN edit path and delegate every write to the flock'd goals module.

Named test_goal_routes (sorts AFTER test_daemon); stdlib-only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "todo_server_under_test", str(REPO / "bin" / "todo-server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GoalRoutesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.srv = _load_server()

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()


class GoalRouteTests(GoalRoutesBase):
    def test_id_regex_mirrors_goals_module(self):
        from assistant import goals
        self.assertEqual(self.srv.GOAL_ID_RE.pattern, goals.GOAL_ID_RE.pattern)

    def test_add_then_list(self):
        ok, msg = self.srv.goal_add(json.dumps(
            {"title": "Ship M5", "outcome": "connectors green",
             "links": {"repos": ["elitecoder/assistant"]}}))
        self.assertTrue(ok, msg)
        self.assertIn("goal-1", msg)
        ok, body = self.srv.goal_list()
        self.assertTrue(ok, body)
        data = json.loads(body)
        self.assertEqual(data["n"], 1)
        self.assertEqual(data["goals"][0]["id"], "goal-1")
        self.assertFalse(data["paused"])

    def test_add_rejects_missing_outcome(self):
        ok, msg = self.srv.goal_add(json.dumps({"title": "no outcome"}))
        self.assertFalse(ok)
        self.assertIn("outcome", msg)

    def test_add_rejects_bad_body(self):
        ok, msg = self.srv.goal_add("not json")
        self.assertFalse(ok)

    def test_update_direct_human_edit(self):
        self.srv.goal_add(json.dumps({"title": "a", "outcome": "o"}))
        ok, msg = self.srv.goal_update("goal-1", json.dumps({"status": "done"}))
        self.assertTrue(ok, msg)
        from assistant import goals
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "done")

    def test_update_unknown_goal_404_message(self):
        ok, msg = self.srv.goal_update("goal-9", json.dumps({"status": "done"}))
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_update_rejects_mechanical_field(self):
        self.srv.goal_add(json.dumps({"title": "a", "outcome": "o"}))
        ok, msg = self.srv.goal_update(
            "goal-1", json.dumps({"lastProgressAt": "2026-01-01T00:00:00Z"}))
        self.assertFalse(ok)

    def test_rerank(self):
        for t in ("a", "b", "c"):
            self.srv.goal_add(json.dumps({"title": t, "outcome": "o"}))
        ok, msg = self.srv.goal_rerank(json.dumps(
            {"order": ["goal-3", "goal-1", "goal-2"]}))
        self.assertTrue(ok, msg)
        from assistant import goals
        ranks = {g["id"]: g["rank"] for g in goals.load_goals()["goals"]}
        self.assertEqual(ranks, {"goal-3": 1, "goal-1": 2, "goal-2": 3})

    def test_goal_id_regex_rejects_bad_ids(self):
        self.assertIsNone(self.srv.GOAL_ID_RE.match("goal-"))
        self.assertIsNone(self.srv.GOAL_ID_RE.match("goal-abc"))
        self.assertIsNone(self.srv.GOAL_ID_RE.match("../etc"))
        self.assertIsNotNone(self.srv.GOAL_ID_RE.match("goal-12"))


if __name__ == "__main__":
    unittest.main()
