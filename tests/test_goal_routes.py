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
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
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


def _load_server_http():
    name = "todo_server_goal_http"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(REPO / "bin" / "todo-server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
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


# ─── M7 kill switch + M18/M15 automation gating (handler level) ──────────────

class GoalPauseAndGatingTests(GoalRoutesBase):
    def test_pause_resume_sets_paused_flag(self):
        from assistant import goals
        self.srv.goal_add(json.dumps({"title": "a", "outcome": "o"}))
        ok, msg = self.srv.goal_set_paused(True)          # the REAL switch (m7)
        self.assertTrue(ok, msg)
        self.assertTrue(goals.load_goals()["_paused"])
        ok, _ = self.srv.goal_set_paused(False)
        self.assertTrue(ok)
        self.assertFalse(goals.load_goals()["_paused"])

    def test_automation_status_edit_files_proposal_not_applied(self):
        from assistant import goals
        self.srv.goal_add(json.dumps({"title": "a", "outcome": "o"}))
        ok, msg = self.srv.goal_update(
            "goal-1", json.dumps({"status": "done"}), is_human=False)
        self.assertTrue(ok, msg)
        self.assertIn("proposal", msg)
        # NOT applied — the design's central automation invariant (m18)
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "active")
        lines = goals.proposals_path().read_text().splitlines()
        self.assertTrue(any('"goal_update"' in ln for ln in lines))

    def test_human_status_edit_applies(self):
        from assistant import goals
        self.srv.goal_add(json.dumps({"title": "a", "outcome": "o"}))
        ok, msg = self.srv.goal_update(
            "goal-1", json.dumps({"status": "done"}), is_human=True)
        self.assertTrue(ok, msg)
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "done")

    def test_non_sensitive_edit_applies_without_assertion(self):
        from assistant import goals
        self.srv.goal_add(json.dumps({"title": "a", "outcome": "o"}))
        ok, msg = self.srv.goal_update(
            "goal-1", json.dumps({"outcome": "new measurable"}), is_human=False)
        self.assertTrue(ok, msg)
        self.assertEqual(goals.load_goals()["goals"][0]["outcome"],
                         "new measurable")


# ─── M4 CSRF + M18 over the real HTTP surface ────────────────────────────────

class GoalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_server_http()
        cls._orig_r = cls.mod.rerender
        cls._orig_rd = cls.mod.rerender_dashboard
        cls.mod.rerender = lambda: None
        cls.mod.rerender_dashboard = lambda: None
        cls.httpd = HTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.mod.rerender = cls._orig_r
        cls.mod.rerender_dashboard = cls._orig_rd

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        from assistant import goals
        goals.add_goal(title="a", outcome="o")

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def _post(self, path, *, origin=None, human=False, body=b""):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        if origin is not None:
            req.add_header("Origin", origin)
        if human:
            req.add_header("X-Assistant-Human", "1")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_cross_origin_goal_add_refused_403(self):
        code, _ = self._post(
            "/goal/add", origin="http://evil.example",
            body=json.dumps({"title": "x", "outcome": "y"}).encode())
        self.assertEqual(code, 403)

    def test_cross_origin_goal_update_refused_403(self):
        code, _ = self._post(
            "/goal/update/goal-1", origin="http://evil.example",
            body=json.dumps({"status": "done"}).encode())
        self.assertEqual(code, 403)

    def test_same_origin_goal_add_allowed(self):
        code, _ = self._post(
            "/goal/add", origin="http://127.0.0.1:9876",
            body=json.dumps({"title": "x", "outcome": "y"}).encode())
        self.assertEqual(code, 200)

    def test_automation_cannot_flip_status_to_done(self):
        # localhost automation shape: no Origin, no human header → files a
        # proposal, does NOT apply (m18 central invariant).
        code, body = self._post(
            "/goal/update/goal-1",
            body=json.dumps({"status": "done"}).encode())
        self.assertEqual(code, 200)
        self.assertIn("proposal", body)
        from assistant import goals
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "active")

    def test_human_header_flips_status(self):
        code, _ = self._post(
            "/goal/update/goal-1", human=True,
            body=json.dumps({"status": "done"}).encode())
        self.assertEqual(code, 200)
        from assistant import goals
        self.assertEqual(goals.load_goals()["goals"][0]["status"], "done")

    def test_pause_route_sets_paused(self):
        code, _ = self._post("/goal/pause")
        self.assertEqual(code, 200)
        from assistant import goals
        self.assertTrue(goals.load_goals()["_paused"])


if __name__ == "__main__":
    unittest.main()
