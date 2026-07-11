"""Golden raw→event replay + the M5 acceptance fixtures (design M5 eval row).

  * Replay: every recorded raw payload in evals/connectors/<name>/fixtures/
    (>=20 per connector) must still normalize to its committed WorldEvent — a
    regression in a connector's normalization is caught here.
  * Acceptance:
      - double-delivery → ONE event (consumer-side dedup; the decision layer is
        1:1 with events, so one event == one decision);
      - kill-mid-batch restart → no loss / no dupes (durable cursor + spine
        dedup make the re-poll safe);
      - token-expiry surfaces in the heartbeat within one poll.

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

from assistant import connector, eventspine  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ghc = _load("ghc_fx", "bin/connectors/github-notifications.py")
gm = _load("gm_fx", "bin/connectors/gmail.py")
ol = _load("ol_fx", "bin/connectors/outlook.py")

GH_FIX = REPO / "evals" / "connectors" / "github" / "fixtures"
GMAIL_FIX = REPO / "evals" / "connectors" / "gmail" / "fixtures"
OUTLOOK_FIX = REPO / "evals" / "connectors" / "outlook" / "fixtures"
OUTLOOK_ACCT = "me@contoso.com"


class GoldenReplayTests(unittest.TestCase):
    def _replay(self, fixtures_dir, normalize, minimum=20):
        files = sorted(fixtures_dir.glob("*.json"))
        self.assertGreaterEqual(
            len(files), minimum,
            msg=f"{fixtures_dir} needs >={minimum} recorded events")
        for f in files:
            data = json.loads(f.read_text())
            got = normalize(data["raw"])
            self.assertEqual(got, data["expected"],
                             msg=f"normalization drift in {f.name}")

    def test_github_fixtures_replay(self):
        self._replay(GH_FIX, ghc.notification_to_event)

    def test_gmail_fixtures_replay(self):
        self._replay(GMAIL_FIX,
                     lambda raw: gm.message_to_event(raw, "mukul@gmail.com"))

    def test_outlook_fixtures_replay(self):
        # M5 wave-3: >=20 recorded Microsoft Graph message shapes must still
        # normalize to their committed WorldEvent (direct/cc/newsletter/message).
        # The floor matches github/gmail (>=20) — the coverage convention is NOT
        # weakened for the new connector (D7).
        self._replay(OUTLOOK_FIX,
                     lambda raw: ol.message_to_event(raw, OUTLOOK_ACCT))

    def test_every_expected_is_wellformed_worldevent(self):
        for d in (GH_FIX, GMAIL_FIX, OUTLOOK_FIX):
            for f in d.glob("*.json"):
                ev = json.loads(f.read_text())["expected"]
                self.assertEqual(ev["schema"], "world-event/1")
                for k in ("source", "kind", "external_id", "id"):
                    self.assertTrue(ev.get(k), msg=f"{f.name} missing {k}")


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


def _notif(i, reason="mention"):
    return {"id": str(i), "reason": reason,
            "updated_at": f"2026-07-10T12:0{i}:00Z",
            "repository": {"full_name": "elitecoder/assistant",
                           "owner": {"login": "elitecoder"}},
            "subject": {"title": f"item {i}", "type": "Issue",
                        "url": f"https://api.github.com/x/{i}"}}


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, method, url, headers=None, data=None):
        assert method == "GET"
        self.calls += 1
        # Same batch on every poll (the GitHub API keeps returning an unread
        # thread until it is marked read — which a read-only connector never
        # does), so a re-poll is a genuine double-delivery.
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class DoubleDeliveryTests(HomeTestCase):
    def test_double_delivery_yields_one_event(self):
        body = json.dumps([_notif(1, "review_requested")]).encode()
        http = FakeHttp([(200, {"Last-Modified": "LM"}, body)])
        c = ghc.GitHubNotificationsConnector(
            token_provider=lambda: "t", http=http)
        # Two independent polls both emit the same notification.
        c.poll_once(now=1)
        c.poll_once(now=2)
        drops = list((self.home / ".assistant/inbox").glob("evt-*.json"))
        self.assertEqual(len(drops), 2)  # at-least-once producer: 2 drops
        # The spine collapses them to exactly ONE event → exactly one decision.
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 1)
        self.assertEqual(summ["inbox_duplicates"], 1)
        rows = [l for l in eventspine.events_path().read_text().splitlines() if l]
        self.assertEqual(len(rows), 1)


class KillMidBatchTests(HomeTestCase):
    def test_kill_before_cursor_save_loses_nothing_dupes_nothing(self):
        body = json.dumps([_notif(1), _notif(2), _notif(3)]).encode()
        http = FakeHttp([(200, {"Last-Modified": "LM"}, body)])
        c = ghc.GitHubNotificationsConnector(
            token_provider=lambda: "t", http=http)

        # Simulate a crash AFTER the events were dropped but BEFORE the cursor
        # watermark is persisted — the durability-critical window.
        orig_save = c.save_cursor

        def boom(cursor):
            raise OSError("killed mid-batch")

        c.save_cursor = boom  # type: ignore
        with self.assertRaises(OSError):
            c.poll_once(now=1)
        # cursor never advanced
        self.assertEqual(c.load_cursor(), {})

        # Restart: fresh connector, same durable state, re-fetches the same
        # batch (nothing was marked read) and re-emits.
        c2 = ghc.GitHubNotificationsConnector(
            token_provider=lambda: "t",
            http=FakeHttp([(200, {"Last-Modified": "LM"}, body)]))
        c2.poll_once(now=2)

        # Drain everything once: 3 distinct events, no loss, no dupes.
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 3)
        ids = {json.loads(l)["external_id"]
               for l in eventspine.events_path().read_text().splitlines() if l}
        self.assertEqual(len(ids), 3)


class TokenExpiryHeartbeatTests(HomeTestCase):
    def test_expiry_surfaces_within_one_poll(self):
        c = gm.GmailConnector()
        tp = c.token_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps({
            "access_token": "OLD", "refresh_token": "R",
            "expiry_epoch": 1000, "client_id": "c", "client_secret": "s"}))

        def oauth(uri, form):
            return {"access_token": "FRESH", "expires_in": 1800}

        http_profile = (200, {}, json.dumps(
            {"historyId": "1", "emailAddress": "me@x.com"}).encode())

        class H:
            calls = []

            def __call__(self, method, url, headers=None, data=None):
                H.calls.append(url)
                return http_profile

        c2 = gm.GmailConnector(http=H(), oauth_transport=oauth)
        now = 1783080000
        c2.poll_once(now=now)
        hb = json.loads(c2.heartbeat_path().read_text())
        self.assertEqual(hb["token_expiry_epoch"], now + 1800)


if __name__ == "__main__":
    unittest.main()
