"""Tests for bin/connectors/github-notifications.py — the M5 GitHub connector.

Read-only producer proven against an injected HTTP transport (no network):
notification→WorldEvent normalization, the Last-Modified watermark cursor, the
304 cheap-no-op path, auth/HTTP error surfacing in the heartbeat, and the
GET-only (never a mutation verb) discipline.

New module (sorts after test_daemon), unittest style, tmp $HOME per test.
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


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ghc = _load("ghc_test", "bin/connectors/github-notifications.py")


class FakeHttp:
    """Records (method, url, headers) and returns queued (status, headers,
    body) responses. Fails the test if a non-GET verb is ever attempted."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url, headers or {}))
        assert method == "GET", f"connector must be read-only, got {method}"
        return self.responses.pop(0)


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old is not None:
            os.environ["HOME"] = self._old
        self._tmp.cleanup()

    def make(self, responses, token="ghtok"):
        http = FakeHttp(responses)
        c = ghc.GitHubNotificationsConnector(
            token_provider=lambda: token, http=http)
        return c, http


def notif(i=1, reason="mention"):
    return {"id": str(i), "reason": reason,
            "updated_at": f"2026-07-10T12:0{i}:00Z",
            "repository": {"full_name": "elitecoder/assistant",
                           "owner": {"login": "elitecoder"}},
            "subject": {"title": f"item {i}", "type": "Issue",
                        "url": f"https://api.github.com/x/{i}"}}


class NormalizeTests(unittest.TestCase):
    def test_external_id_shape(self):
        ev = ghc.notification_to_event(notif(7, "review_requested"))
        self.assertEqual(
            ev["external_id"],
            "gh-notif:elitecoder/assistant:7:2026-07-10T12:07:00Z")
        self.assertEqual(ev["kind"], "review_requested")
        self.assertEqual(ev["source"], "github")
        self.assertEqual(ev["actor"], "elitecoder")

    def test_missing_fields_do_not_crash(self):
        ev = ghc.notification_to_event({"id": "1"})
        self.assertEqual(ev["source"], "github")
        self.assertTrue(ev["external_id"].startswith("gh-notif:?/?:1:"))


class PollTests(HomeTestCase):
    def test_200_emits_events_and_sets_watermark(self):
        body = json.dumps([notif(1, "mention"), notif(2, "review_requested")]
                          ).encode()
        c, http = self.make([(200, {"Last-Modified": "Wed, 10 Jul 2026 12:00:00 GMT"}, body)])
        res = c.poll_once(now=1783080000)
        self.assertEqual(res["emitted"], 2)
        drops = list((self.home / ".assistant/inbox").glob("evt-github-*.json"))
        self.assertEqual(len(drops), 2)
        cur = c.load_cursor()
        self.assertEqual(cur["last_modified"], "Wed, 10 Jul 2026 12:00:00 GMT")

    def test_watermark_replayed_as_if_modified_since(self):
        c, _ = self.make([(200, {"Last-Modified": "LM1"}, b"[]")])
        c.poll_once(now=1)
        c2, http2 = self.make([(304, {}, b"")])
        c2.poll_once(now=2)
        # second connector shares the same cursor file on disk
        self.assertIn("If-Modified-Since", http2.calls[0][2])
        self.assertEqual(http2.calls[0][2]["If-Modified-Since"], "LM1")

    def test_304_is_a_cheap_noop(self):
        # seed a watermark first
        c0, _ = self.make([(200, {"Last-Modified": "LM"}, b"[]")])
        c0.poll_once(now=1)
        c, http = self.make([(304, {}, b"")])
        res = c.poll_once(now=2)
        self.assertEqual(res["status"], "not_modified")
        self.assertEqual(res["emitted"], 0)

    def test_auth_error_surfaces_in_heartbeat(self):
        http = FakeHttp([])
        c = ghc.GitHubNotificationsConnector(
            token_provider=lambda: (_ for _ in ()).throw(RuntimeError("no gh")),
            http=http)
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "auth_error")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertTrue(any("token" in e for e in hb["errors"]))
        self.assertEqual(http.calls, [])  # never hit the API without a token

    def test_403_surfaces_in_heartbeat(self):
        c, _ = self.make([(403, {}, b"forbidden")])
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "status_403")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])

    def test_heartbeat_written_on_success(self):
        c, _ = self.make([(200, {"Last-Modified": "LM"},
                           json.dumps([notif(1)]).encode())])
        c.poll_once(now=1783080000)
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["source"], "github")
        self.assertEqual(hb["event_count"], 1)
        self.assertTrue(hb["ok"])


if __name__ == "__main__":
    unittest.main()
