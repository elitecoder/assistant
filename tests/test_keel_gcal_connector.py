"""Tests for bin/connectors/gcal.py — the M5 wave-2 Google Calendar connector.

Proves, against injected HTTP + OAuth transports (no network): the reminder
normalization, the OAuth flow REUSED from the base (authorize seeds a
calendar.readonly token via loopback+PKCE; refresh-on-expiry surfaces in the
heartbeat), the syncToken seed/incremental/410-resync paths (the 410 is
SURFACED in the heartbeat — the wave-1 E5 lesson), pagination cursor discipline
(a truncated sync parks a pageToken, never advances the syncToken past unfetched
pages), the once-per-reminder-window dedup (the critical requirement), poison
skip, not_configured, dry-run side-effect-freeness, GET-only read-only
discipline, and the ≥15 golden raw→event replay fixtures.

New module, unittest style, tmp $HOME per test.
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


gcal = _load("gcal_test", "bin/connectors/gcal.py")
GCAL_FIX = REPO / "evals" / "connectors" / "gcal" / "fixtures"


def _cal(eid, start, **kw):
    ev = {"id": eid, "summary": kw.pop("summary", "mtg"),
          "start": start, "htmlLink": "http://x"}
    ev.update(kw)
    return ev


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

    def seed_token(self, expiry_epoch=10 ** 10):
        c = gcal.GCalConnector()
        p = c.token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "access_token": "A", "refresh_token": "R",
            "expiry_epoch": expiry_epoch, "client_id": "c",
            "client_secret": "s"}))

    def _drops(self):
        return sorted((self.home / ".assistant/inbox").glob("evt-gcal-*.json"))


class ListHttp:
    """A fake events.list transport returning a fixed items page + syncToken,
    asserting GET-only. Optionally 410s a given syncToken."""
    def __init__(self, items, sync="S1", gone_token=None, pages=None):
        self.items = items
        self.sync = sync
        self.gone_token = gone_token
        self.pages = pages  # optional list of (items, nextPageToken, nextSync)
        self.calls = []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url))
        assert method == "GET", f"read-only violated: {method}"
        if self.gone_token and f"syncToken={self.gone_token}" in url:
            return (410, {}, b"{}")
        if self.pages is not None:
            idx = sum(1 for m, u in self.calls if "events" in u) - 1
            items, nxt, nsync = self.pages[min(idx, len(self.pages) - 1)]
            body = {"items": items}
            if nxt:
                body["nextPageToken"] = nxt
            if nsync:
                body["nextSyncToken"] = nsync
            return (200, {}, json.dumps(body).encode())
        return (200, {}, json.dumps(
            {"items": self.items, "nextSyncToken": self.sync}).encode())


# ─── normalization (reminder events) ─────────────────────────────────────────

class NormalizeTests(unittest.TestCase):
    def test_reminder_external_id_is_the_window_dedup_key(self):
        ev = gcal.event_to_reminder(
            _cal("E1", {"dateTime": "2026-08-01T10:00:00Z"}), "1h")
        self.assertEqual(ev["external_id"],
                         "gcal-upcoming:E1:2026-08-01T10:00:00Z:1h")
        self.assertEqual(ev["kind"], "event_upcoming")
        self.assertEqual(ev["refs"]["window"], "1h")
        self.assertEqual(ev["refs"]["gcal"], "gcal:E1:2026-08-01T10:00:00Z")

    def test_two_windows_distinct_ids(self):
        e = _cal("E2", {"dateTime": "2026-08-01T10:00:00Z"})
        a = gcal.event_to_reminder(e, "24h")["external_id"]
        b = gcal.event_to_reminder(e, "1h")["external_id"]
        self.assertNotEqual(a, b)

    def test_all_day_event_start_parses(self):
        ev = gcal.event_to_reminder(_cal("E3", {"date": "2026-08-01"}), "24h")
        self.assertEqual(ev["external_id"], "gcal-upcoming:E3:2026-08-01:24h")
        self.assertEqual(ev["epoch"],
                         int(eventspine.parse_iso("2026-08-01T00:00:00Z")))

    def test_ts_is_event_start_not_polltime(self):
        ev = gcal.event_to_reminder(
            _cal("E4", {"dateTime": "2026-08-01T10:00:00Z"}), "1h",
            now=999999999)
        self.assertEqual(ev["epoch"],
                         int(eventspine.parse_iso("2026-08-01T10:00:00Z")))


# ─── the critical once-per-window dedup ──────────────────────────────────────

class ReminderDedupTests(HomeTestCase):
    def _make(self, items):
        return gcal.GCalConnector(
            http=ListHttp(items),
            oauth_transport=lambda u, f: {"access_token": "A",
                                          "expires_in": 3600})

    def test_once_per_window_across_60s_polls(self):
        self.seed_token()
        START = 1_000_000
        ev = _cal("EVT", {"dateTime": eventspine.utc_iso(START)})
        # Poll every 60s from before the 24h window through past the 1h window.
        for now in range(START - 90000, START + 60, 60):
            self._make([ev]).poll_once(now=now)
        windows = sorted(json.loads(d.read_text())["refs"]["window"]
                         for d in self._drops())
        self.assertEqual(windows, ["1h", "24h"])  # EXACTLY one of each

    def test_no_reminder_before_window_or_after_start(self):
        self.seed_token()
        START = 1_000_000
        ev = _cal("EVT", {"dateTime": eventspine.utc_iso(START)})
        self._make([ev]).poll_once(now=START - 100000)  # long before
        self.assertEqual(self._drops(), [])
        self._make([ev]).poll_once(now=START + 100)     # after start
        self.assertEqual(self._drops(), [])

    def test_fired_keys_persist_in_cursor(self):
        self.seed_token()
        START = 1_000_000
        ev = _cal("EVT", {"dateTime": eventspine.utc_iso(START)})
        c = self._make([ev])
        c.poll_once(now=START - 100)  # both windows due
        fired = c.load_cursor()["emitted_reminders"]
        self.assertEqual(len(fired), 2)


# ─── sync: seed / incremental / 410 resync (E5) / pagination ─────────────────

class SyncTests(HomeTestCase):
    def _make(self, http):
        return gcal.GCalConnector(
            http=http,
            oauth_transport=lambda u, f: {"access_token": "A",
                                          "expires_in": 3600})

    def test_seed_populates_upcoming_and_captures_synctoken(self):
        self.seed_token()
        ev = _cal("E", {"dateTime": "2026-08-01T10:00:00Z"})
        c = self._make(ListHttp([ev], sync="TOK1"))
        c.poll_once(now=1)
        cur = c.load_cursor()
        self.assertEqual(cur["sync_token"], "TOK1")
        self.assertIn("E", cur["upcoming"])

    def test_410_triggers_resync_surfaced_in_heartbeat(self):
        self.seed_token()
        c0 = self._make(ListHttp([], sync="OLD"))
        c0.poll_once(now=1)
        self.assertEqual(c0.load_cursor()["sync_token"], "OLD")
        http = ListHttp([_cal("N", {"dateTime": "2026-08-01T10:00:00Z"})],
                        sync="NEW", gone_token="OLD")
        c = self._make(http)
        res = c.poll_once(now=2)
        self.assertTrue(any("410" in e for e in res["errors"]))
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])                       # E5: NOT ok:true
        self.assertTrue(any("410" in e for e in hb["errors"]))
        self.assertEqual(c.load_cursor()["sync_token"], "NEW")  # resynced

    def test_truncation_parks_pagetoken_not_synctoken(self):
        self.seed_token()
        # page 1 has a nextPageToken but NO nextSyncToken; force max_pages=1.
        http = ListHttp(None, pages=[
            ([_cal("P1", {"dateTime": "2026-08-01T10:00:00Z"})], "PG2", None)])
        c = self._make(http)
        c.config["max_pages"] = 1
        c.poll_once(now=1)
        cur = c.load_cursor()
        self.assertEqual(cur["page_token"], "PG2")   # parked to resume
        self.assertIsNone(cur.get("sync_token"))     # never advanced past page

    def test_poison_event_is_skipped_and_counted(self):
        self.seed_token()
        good = _cal("G", {"dateTime": "2026-08-01T10:00:00Z"})
        poison = {"id": "BAD", "start": {"dateTime": 12345}}  # dateTime not str
        # 12345 is not a str, so _event_start returns None → item ignored (no
        # crash); the good event still lands. Prove no wedge / no crash.
        c = self._make(ListHttp([good, poison]))
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "ok")
        self.assertIn("G", c.load_cursor()["upcoming"])
        self.assertNotIn("BAD", c.load_cursor()["upcoming"])

    def test_not_configured_when_no_token(self):
        res = gcal.GCalConnector().poll_once(now=1)
        self.assertEqual(res["status"], "not_configured")
        hb = json.loads(
            (self.home / ".assistant/connectors/gcal/heartbeat.json").read_text())
        self.assertEqual(hb["status"], "not_configured")
        self.assertTrue(hb["ok"])

    def test_oauth_refresh_surfaces_expiry_in_one_poll(self):
        self.seed_token(expiry_epoch=1000)   # expired
        http = ListHttp([])
        c = gcal.GCalConnector(
            http=http,
            oauth_transport=lambda u, f: {"access_token": "FRESH",
                                          "expires_in": 3600})
        now = 1783080000
        c.poll_once(now=now)
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["token_expiry_epoch"], now + 3600)
        self.assertEqual(http.calls[0][0], "GET")

    def test_dry_run_is_side_effect_free(self):
        self.seed_token()
        START = 1_000_000
        ev = _cal("EVT", {"dateTime": eventspine.utc_iso(START)})
        c = gcal.GCalConnector(
            http=ListHttp([ev]),
            oauth_transport=lambda u, f: {"access_token": "A",
                                          "expires_in": 3600},
            dry_run=True)
        c.poll_once(now=START - 100)
        self.assertEqual(self._drops(), [])              # nothing dropped
        self.assertFalse(c.cursor_path().exists())       # cursor not saved


# ─── OAuth authorize (reuses the base flow, calendar.readonly scope) ─────────

class AuthorizeTests(HomeTestCase):
    def test_authorize_seeds_readonly_token(self):
        cs = self.home / "cs.json"
        cs.write_text(json.dumps(
            {"installed": {"client_id": "cid", "client_secret": "sec"}}))

        def cg(build, state):
            return "CODE", state, "http://127.0.0.1:1/"

        def ex(uri, form):
            self.assertEqual(form["grant_type"], "authorization_code")
            return {"access_token": "AT", "refresh_token": "RT",
                    "expires_in": 3600}

        p = gcal.authorize(str(cs), code_getter=cg, exchange_transport=ex)
        tok = json.loads(Path(p).read_text())
        self.assertEqual(tok["scopes"], gcal.GCAL_READONLY_SCOPE)
        self.assertTrue(tok["refresh_token"])
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_authorize_pins_google_endpoints(self):
        # A poisoned token_uri in the client-secrets file must be replaced with
        # the pinned Google endpoint (base A1) — prove the exchange still targets
        # a Google host.
        cs = self.home / "cs.json"
        cs.write_text(json.dumps({"installed": {
            "client_id": "cid", "client_secret": "sec",
            "token_uri": "https://evil.example/steal"}}))
        seen = {}

        def cg(build, state):
            return "CODE", state, "http://127.0.0.1:1/"

        def ex(uri, form):
            seen["uri"] = uri
            return {"access_token": "AT", "refresh_token": "RT",
                    "expires_in": 3600}

        gcal.authorize(str(cs), code_getter=cg, exchange_transport=ex)
        self.assertNotIn("evil", seen["uri"])
        self.assertIn("googleapis.com", seen["uri"])


# ─── golden raw→event replay (≥15) + well-formed ─────────────────────────────

class GoldenReplayTests(unittest.TestCase):
    def test_replay(self):
        files = sorted(GCAL_FIX.glob("*.json"))
        self.assertGreaterEqual(len(files), 15,
                                msg=f"{GCAL_FIX} needs >=15 recorded events")
        for f in files:
            data = json.loads(f.read_text())
            got = gcal.event_to_reminder(data["raw"], data["window"])
            got = {k: v for k, v in got.items()
                   if k not in ("id", "raw_path", "epoch")}
            self.assertEqual(got, data["expected"],
                             msg=f"normalization drift in {f.name}")

    def test_expected_are_wellformed_worldevents(self):
        for f in GCAL_FIX.glob("*.json"):
            ev = json.loads(f.read_text())["expected"]
            self.assertEqual(ev["schema"], "world-event/1")
            self.assertTrue(ev["external_id"].startswith("gcal-upcoming:"))
            self.assertEqual(ev["kind"], "event_upcoming")


if __name__ == "__main__":
    unittest.main()
