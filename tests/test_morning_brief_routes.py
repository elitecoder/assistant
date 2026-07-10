"""Tests for the Keel M3 todo-server routes: POST /brief/seen (the Brief-tab
view signal → seen sidecar) and the wrong_lane decision action (transition
to rejected + confirmation-gated type=policy proposal via the M2 proposal
machinery). Same loopback-HTTPServer harness as test_decision_routes.py;
brief/decisions/policy resolve paths per call, so each test gets a tmp $HOME.

Named test_morning_brief_routes (not test_brief_routes) for the same
discovery-order reason as test_morning_brief.py — see its docstring.
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
from datetime import datetime
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import brief, decisions  # noqa: E402

NOW = datetime(2026, 7, 2, 10, 0).timestamp()


def _load_server():
    name = "todo_server_brief_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(REPO / "bin" / "todo-server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_event(i=1, source="cmux", kind="stale_kind", **over) -> dict:
    ev = {"schema": "world-event/1", "id": f"eid-{i}", "source": source,
          "kind": kind, "external_id": f"{source}:{kind}:{i}",
          "title": f"event {i}", "snippet": "s",
          "refs": {"ws_ref": f"workspace:{i}"}, "raw_path": None}
    ev.update(over)
    return ev


class BriefRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_server()
        cls._orig_rerender = cls.mod.rerender_dashboard
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
        cls.mod.rerender_dashboard = cls._orig_rerender

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def post(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()

    # ─── /brief/seen ─────────────────────────────────────────────────────

    def test_brief_seen_stamps_sidecar(self):
        brief.write_brief(brief.build_brief(now=NOW))
        date = brief.local_date(NOW)
        code, msg = self.post(f"/brief/seen?date={date}")
        self.assertEqual(code, 200, msg)
        sidecar = brief.seen_path(date)
        self.assertTrue(sidecar.exists())
        self.assertEqual(json.loads(sidecar.read_text())["date"], date)
        # Idempotent second view.
        code2, msg2 = self.post(f"/brief/seen?date={date}")
        self.assertEqual(code2, 200)
        self.assertIn("already seen", msg2)

    def test_brief_seen_defaults_to_latest_brief(self):
        brief.write_brief(brief.build_brief(now=NOW))
        code, msg = self.post("/brief/seen")
        self.assertEqual(code, 200, msg)
        self.assertTrue(brief.seen_path(brief.local_date(NOW)).exists())

    def test_brief_seen_without_brief_404s(self):
        code, msg = self.post("/brief/seen")
        self.assertEqual(code, 404)
        self.assertIn("no brief", msg)

    def test_brief_seen_rejects_bad_date(self):
        code, msg = self.post("/brief/seen?date=not-a-date")
        self.assertEqual(code, 400)
        self.assertIn("invalid date", msg)

    # ─── wrong_lane ──────────────────────────────────────────────────────

    def proposals(self):
        p = self.home / ".assistant/comms/proposals.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines()]

    def test_wrong_lane_rejects_and_files_policy_proposal(self):
        rec, _ = decisions.open_decision(
            event=make_event(1), lane="staged", policy_id="rule-x", now=NOW)
        code, msg = self.post(f"/decision/act/{rec['id']}?action=wrong_lane")
        self.assertEqual(code, 200, msg)
        self.assertIn("rejected", msg)
        self.assertIn("policy proposal filed", msg)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "rejected")
        props = self.proposals()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["type"], "policy")
        self.assertEqual(props[0]["status"], "pending")
        self.assertEqual(props[0]["source"], "wrong-lane-tap")
        pp = props[0]["proposed_policy"]
        self.assertEqual(pp["match"], {"source": "cmux", "kind": "stale_kind"})
        self.assertIsNone(pp["lane"])  # the tap says "not THIS lane" only
        self.assertEqual(props[0]["evidence"]["decision_id"], rec["id"])
        self.assertEqual(props[0]["evidence"]["previous_lane"], "staged")

    def test_wrong_lane_dedups_pending_proposals(self):
        rec1, _ = decisions.open_decision(
            event=make_event(1), lane="staged", policy_id="rule-x", now=NOW)
        rec2, _ = decisions.open_decision(
            event=make_event(2), lane="staged", policy_id="rule-x", now=NOW)
        self.post(f"/decision/act/{rec1['id']}?action=wrong_lane")
        code, msg = self.post(f"/decision/act/{rec2['id']}?action=wrong_lane")
        self.assertEqual(code, 200)
        self.assertIn("already pending", msg)
        # Both decisions rejected, but only ONE proposal for the (source, kind).
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec2["id"]]["status"], "rejected")
        self.assertEqual(len(self.proposals()), 1)

    def test_unknown_action_still_400s(self):
        rec, _ = decisions.open_decision(
            event=make_event(1), lane="staged", policy_id="rule-x", now=NOW)
        code, msg = self.post(f"/decision/act/{rec['id']}?action=yeet")
        self.assertEqual(code, 400)
        self.assertIn("unknown action", msg)
        self.assertIn("wrong_lane", msg)  # advertised in the closed vocabulary


if __name__ == "__main__":
    unittest.main()
