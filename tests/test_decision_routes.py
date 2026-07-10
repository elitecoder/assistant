"""Tests for bin/todo-server.py's /decision/* routes (Keel M2): POST-only,
dec-id-regex validated, and every mutation funneled through
src/assistant/decisions.transition (append-only + ledgered).

Exercised over a real loopback HTTPServer like test_todo_server.py, but in
unittest style so `python3 -m unittest discover tests` runs it. The decisions
store lives under a tmp $HOME (decisions.py resolves paths per call).
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

from assistant import decisions  # noqa: E402

NOW = 1783000000.0


def _load_server():
    name = "todo_server_decision_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(REPO / "bin" / "todo-server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_event(i=1, **over) -> dict:
    ev = {"schema": "world-event/1", "id": f"eid-{i}", "source": "cmux",
          "kind": "needs_input",
          "external_id": f"cmux:workspace:{i}:needs_input:1:aa",
          "title": f"workspace:{i} needs_input", "snippet": "approve?",
          "refs": {"ws_ref": f"workspace:{i}"}, "raw_path": None}
    ev.update(over)
    return ev


class DecisionRouteTests(unittest.TestCase):
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

    # ── helpers ──────────────────────────────────────────────────────────

    def _request(self, path, method="POST", body=b""):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _open_decision(self, i=1, lane="escalate"):
        rec, _ = decisions.open_decision(
            event=make_event(i), lane=lane, policy_id="r1", urgency="now",
            now=NOW)
        return rec

    # ── /decision/list ───────────────────────────────────────────────────

    def test_list_is_post_only(self):
        code, _ = self._request("/decision/list", method="GET")
        self.assertEqual(code, 404)

    def test_list_returns_open_decisions(self):
        rec = self._open_decision()
        code, body = self._request("/decision/list")
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertEqual(payload["n_open"], 1)
        self.assertEqual(payload["open"][0]["id"], rec["id"])

    def test_list_empty_store_is_still_ok(self):
        code, body = self._request("/decision/list")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["n_open"], 0)

    # ── /decision/act ────────────────────────────────────────────────────

    def test_act_accept_transitions_and_ledgers(self):
        rec = self._open_decision()
        code, body = self._request(
            f"/decision/act/{rec['id']}?action=accept")
        self.assertEqual(code, 200)
        self.assertIn("accepted", body)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "accepted")
        self.assertEqual(folded[rec["id"]]["resolution"]["via"],
                         "todo-server:accept")
        ledger = [json.loads(l) for l in
                  (self.home / ".assistant/actions-ledger.jsonl")
                  .read_text().splitlines()]
        self.assertTrue(any(r.get("kind") == "decision-transition"
                            and "open->accepted" in r.get("key", "")
                            for r in ledger))

    def test_act_reject_and_edit_with_note(self):
        rec = self._open_decision()
        code, _ = self._request(f"/decision/act/{rec['id']}?action=edit",
                                body=b"different wording please")
        self.assertEqual(code, 200)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "edited")
        self.assertEqual(folded[rec["id"]]["resolution"]["note"],
                         "different wording please")

    def test_act_snooze_records_wake_ts(self):
        rec = self._open_decision()
        code, _ = self._request(
            f"/decision/act/{rec['id']}?action=snooze&minutes=45")
        self.assertEqual(code, 200)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "snoozed")
        self.assertGreater(folded[rec["id"]]["wake_ts"], NOW)

    def test_act_is_post_only(self):
        rec = self._open_decision()
        code, _ = self._request(f"/decision/act/{rec['id']}?action=accept",
                                method="GET")
        self.assertEqual(code, 404)

    def test_act_invalid_id_is_rejected_by_regex(self):
        for bad in ("dec-XYZ", "dec-", "td-101", "dec-..escape",
                    "dec-" + "a" * 65):
            code, body = self._request(
                f"/decision/act/{bad}?action=accept")
            self.assertEqual(code, 400, msg=bad)
            self.assertIn("invalid decision id", body)

    def test_act_unknown_action_is_rejected(self):
        rec = self._open_decision()
        code, body = self._request(
            f"/decision/act/{rec['id']}?action=auto_done")
        self.assertEqual(code, 400)
        self.assertIn("unknown action", body)
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(folded[rec["id"]]["status"], "open")

    def test_act_missing_decision_is_404(self):
        code, body = self._request(
            "/decision/act/dec-0123456789abcdef?action=accept")
        self.assertEqual(code, 404)
        self.assertIn("not found", body)

    def test_act_resolved_decision_is_rejected(self):
        rec = self._open_decision()
        decisions.transition(rec["id"], "accepted", via="test", now=NOW + 5)
        code, body = self._request(
            f"/decision/act/{rec['id']}?action=reject")
        self.assertEqual(code, 400)
        self.assertIn("only open/snoozed", body)

    # ── CORS origin allowlist (exact match) ──────────────────────────────

    def _request_with_origin(self, path, origin, method="POST"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=b"", method=method)
        req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    def test_evil_origin_prefix_gets_no_acao_echo(self):
        # The prefix-match hole: "http://localhost.evil.com".startswith(
        # "http://localhost") is True. Exact matching must refuse it — no
        # Access-Control-Allow-Origin header at all.
        for evil in ("http://localhost.evil.com:9876",
                     "http://localhost.evil.com",
                     "http://127.0.0.1.evil.com",
                     "https://evil.example",
                     "http://localhost:9999"):
            _, headers = self._request_with_origin("/decision/list", evil)
            self.assertNotIn("Access-Control-Allow-Origin", headers,
                             msg=evil)

    def test_allowed_origins_get_exact_acao_echo(self):
        for good in ("http://127.0.0.1:9876", "http://localhost:9876",
                     "null"):
            _, headers = self._request_with_origin("/decision/list", good)
            self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                             good, msg=good)

    # ── /decision/list snippet cap ───────────────────────────────────────

    def test_list_snippet_capped_at_120_chars(self):
        rec, _ = decisions.open_decision(
            event=make_event(1, snippet="S" * 400), lane="escalate",
            policy_id="r1", urgency="now", now=NOW)
        code, body = self._request("/decision/list")
        self.assertEqual(code, 200)
        row = json.loads(body)["open"][0]
        self.assertEqual(len(row["snippet"]), self.mod.LIST_SNIPPET_MAX)
        self.assertEqual(self.mod.LIST_SNIPPET_MAX, 120)
        # The store keeps the full snippet — only the wire view is capped.
        folded = decisions.fold(decisions.read_log())
        self.assertEqual(len(folded[rec["id"]]["snippet"]), 400)


if __name__ == "__main__":
    unittest.main()
