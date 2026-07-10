"""Tests for bin/connectors/gmail.py — the M5 Gmail (readonly) connector.

Proves, against injected HTTP + OAuth transports (no network): mechanical
kind derivation (newsletter/direct/message from Gmail's own labels/headers),
the OAuth refresh flow owned by the base (refresh-on-expiry, expiry surfaced
in heartbeat within one poll), the history-cursor seed/incremental/404-reseed
paths, and GET-only read-only discipline.

New module (sorts after test_daemon), unittest style, tmp $HOME per test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
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


gm = _load("gm_test", "bin/connectors/gmail.py")
ACCT = "mukul@gmail.com"


def _msg(mid, headers, labels=None, snippet="", internal="1783000000000"):
    return {"id": mid, "threadId": "t" + mid, "labelIds": labels or ["INBOX"],
            "internalDate": internal, "snippet": snippet,
            "payload": {"headers": [{"name": k, "value": v}
                                    for k, v in headers.items()]}}


class RouterHttp:
    """URL-routed fake transport. Records every call and asserts GET-only."""
    def __init__(self, *, profile=None, history=None, messages=None):
        self.profile = profile
        self.history = history or (200, {}, b'{"history":[],"historyId":"1"}')
        self.messages = messages or {}
        self.calls = []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url, headers or {}))
        assert method == "GET", f"connector must be read-only, got {method}"
        if "/profile" in url:
            return self.profile
        if "/history" in url:
            return self.history
        if "/messages/" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            return self.messages.get(mid, (404, {}, b"{}"))
        raise AssertionError("unexpected url " + url)


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

    def seed_token(self, *, expiry_epoch=1000, access="OLD"):
        c = gm.GmailConnector()  # to compute the token path under this $HOME
        p = c.token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "access_token": access, "refresh_token": "R",
            "expiry_epoch": expiry_epoch, "client_id": "cid",
            "client_secret": "sec"}))
        return p

    def make(self, http, *, oauth_expires_in=3600):
        def oauth(uri, form):
            return {"access_token": "FRESH", "expires_in": oauth_expires_in}
        return gm.GmailConnector(http=http, oauth_transport=oauth)


# ─── normalization (mechanical kind) ─────────────────────────────────────────

class NormalizeTests(unittest.TestCase):
    def test_newsletter_via_list_unsubscribe(self):
        ev = gm.message_to_event(_msg(
            "a", {"From": "N <n@x.com>", "Subject": "Deal",
                  "List-Unsubscribe": "<mailto:u@x.com>"}), ACCT)
        self.assertEqual(ev["kind"], "newsletter")
        self.assertEqual(ev["external_id"], "gmail:a")

    def test_newsletter_via_category_label(self):
        ev = gm.message_to_event(_msg(
            "b", {"From": "N <n@x.com>", "Subject": "Promo"},
            labels=["INBOX", "CATEGORY_PROMOTIONS"]), ACCT)
        self.assertEqual(ev["kind"], "newsletter")

    def test_direct_when_account_in_to(self):
        ev = gm.message_to_event(_msg(
            "c", {"From": "P <p@x.com>", "To": ACCT, "Subject": "hi"}), ACCT)
        self.assertEqual(ev["kind"], "direct")

    def test_direct_when_account_in_cc(self):
        ev = gm.message_to_event(_msg(
            "d", {"From": "P <p@x.com>", "To": "team@x.com", "Cc": ACCT,
                  "Subject": "hi"}), ACCT)
        self.assertEqual(ev["kind"], "direct")

    def test_generic_message_otherwise(self):
        ev = gm.message_to_event(_msg(
            "e", {"From": "B <b@x.com>", "To": "ops@x.com", "Subject": "n"}),
            ACCT)
        self.assertEqual(ev["kind"], "message")

    def test_sec2_directly_addressed_security_mail_is_direct_not_newsletter(self):
        # SEC2: an SSH-key-added / unusual-sign-in alert carries a
        # List-Unsubscribe header yet is addressed straight To:me. It must NOT
        # be tagged newsletter (which the policy drops) — direct-addressed wins.
        ev = gm.message_to_event(_msg(
            "sec", {"From": "GitHub <noreply@github.com>", "To": ACCT,
                    "Subject": "[GitHub] A new SSH key was added",
                    "List-Unsubscribe": "<mailto:u@github.com>"}), ACCT)
        self.assertEqual(ev["kind"], "direct")

    def test_sec2_newsletter_still_newsletter_when_not_addressed_to_me(self):
        ev = gm.message_to_event(_msg(
            "nl", {"From": "N <n@x.com>", "To": "list@x.com",
                   "Subject": "Digest", "List-Unsubscribe": "<mailto:u>"}),
            ACCT)
        self.assertEqual(ev["kind"], "newsletter")

    def test_n1_sender_ref_populated(self):
        ev = gm.message_to_event(_msg(
            "r", {"From": "Alice <alice@corp.example>", "To": "t@x.com",
                  "Subject": "hi"}), ACCT)
        self.assertEqual(ev["refs"], {"sender": "alice@corp.example"})

    def test_internaldate_becomes_ts(self):
        ev = gm.message_to_event(_msg(
            "f", {"From": "x", "Subject": "y"}, internal="1783000000000"), ACCT)
        self.assertEqual(ev["epoch"], 1783000000)


# ─── OAuth + heartbeat + cursor ──────────────────────────────────────────────

class PollTests(HomeTestCase):
    def test_seed_run_emits_nothing_and_stores_history_id(self):
        self.seed_token(expiry_epoch=10**9)  # not expired
        http = RouterHttp(profile=(
            200, {}, json.dumps({"historyId": "555",
                                 "emailAddress": ACCT}).encode()))
        c = self.make(http)
        res = c.poll_once(now=1783080000)
        self.assertEqual(res["status"], "seeded")
        self.assertEqual(res["emitted"], 0)
        self.assertEqual(c.load_cursor()["history_id"], "555")
        self.assertEqual(c.load_cursor()["email"], ACCT)

    def test_refresh_on_expiry_and_expiry_in_heartbeat_one_poll(self):
        self.seed_token(expiry_epoch=1000)   # expired at now=1783080000
        http = RouterHttp(profile=(
            200, {}, json.dumps({"historyId": "1", "emailAddress": ACCT}
                                ).encode()))
        c = self.make(http, oauth_expires_in=3600)
        now = 1783080000
        c.poll_once(now=now)
        hb = json.loads(c.heartbeat_path().read_text())
        # token_expiry surfaces in the heartbeat within this single poll
        self.assertEqual(hb["token_expiry_epoch"], now + 3600)
        self.assertTrue(hb["ok"])
        # the API call carried the FRESH access token, not the expired one
        auth = http.calls[0][2]["Authorization"]
        self.assertEqual(auth, "Bearer FRESH")

    def test_incremental_emits_and_advances_history(self):
        self.seed_token(expiry_epoch=10**9)
        c0 = self.make(RouterHttp(profile=(
            200, {}, json.dumps({"historyId": "100",
                                 "emailAddress": ACCT}).encode())))
        c0.poll_once(now=1)  # seed → history_id=100
        hist = (200, {}, json.dumps({
            "historyId": "140",
            "history": [{"messagesAdded": [{"message": {"id": "m1"}}]},
                        {"messagesAdded": [{"message": {"id": "m2"}}]}]}).encode())
        messages = {
            "m1": (200, {}, json.dumps(_msg(
                "m1", {"From": "P <p@x.com>", "To": ACCT, "Subject": "s1"})).encode()),
            "m2": (200, {}, json.dumps(_msg(
                "m2", {"From": "N <n@x.com>", "Subject": "s2",
                       "List-Unsubscribe": "<x>"})).encode()),
        }
        c = self.make(RouterHttp(history=hist, messages=messages))
        res = c.poll_once(now=2)
        self.assertEqual(res["emitted"], 2)
        self.assertEqual(c.load_cursor()["history_id"], "140")
        drops = sorted((self.home / ".assistant/inbox").glob("evt-gmail-*.json"))
        kinds = sorted(json.loads(d.read_text())["kind"] for d in drops)
        self.assertEqual(kinds, ["direct", "newsletter"])

    def test_history_404_reseeds(self):
        self.seed_token(expiry_epoch=10**9)
        c0 = self.make(RouterHttp(profile=(
            200, {}, json.dumps({"historyId": "9",
                                 "emailAddress": ACCT}).encode())))
        c0.poll_once(now=1)
        c = self.make(RouterHttp(
            history=(404, {}, b"{}"),
            profile=(200, {}, json.dumps({"historyId": "77",
                                          "emailAddress": ACCT}).encode())))
        res = c.poll_once(now=2)
        self.assertEqual(res["status"], "seeded")
        self.assertEqual(c.load_cursor()["history_id"], "77")

    def test_oauth_error_surfaces_in_heartbeat_no_http(self):
        self.seed_token(expiry_epoch=1000)
        http = RouterHttp()

        def bad_oauth(uri, form):
            raise gm.connector.OAuthError("refresh 400")

        c = gm.GmailConnector(http=http, oauth_transport=bad_oauth)
        res = c.poll_once(now=1783080000)
        self.assertEqual(res["status"], "oauth_error")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertEqual(http.calls, [])  # never hit Gmail without a token


# ─── cursor-discipline: at-least-once, no event loss (BLOCKER E1/E2 + E4/E5) ──

class HistoryHttp:
    """A history-aware fake: users.history.list returns the records whose
    per-change id is > startHistoryId (Gmail's real semantics), so a cursor
    that advanced to record R re-fetches ONLY R+1… next poll. messages.get is
    controllable per-id: transient failures (429), 404-gone, poison payloads."""
    def __init__(self, records, *, msg_status=None, poison=(), gone=(),
                 fail_once=()):
        # records: ordered list of (record_id:int, message_id:str)
        self.records = list(records)
        self.newest = str(max((r for r, _ in records), default=0))
        self.msg_status = dict(msg_status or {})
        self.poison = set(poison)
        self.gone = set(gone)
        self.fail_once = set(fail_once)
        self._failed = set()
        self.calls = []

    def __call__(self, method, url, headers=None, data=None):
        assert method == "GET", f"read-only violated: {method}"
        self.calls.append(url)
        if "/history" in url:
            start = int(re.search(r"startHistoryId=(\d+)", url).group(1))
            recs = [{"id": str(rid),
                     "messagesAdded": [{"message": {"id": mid}}]}
                    for rid, mid in self.records if rid > start]
            return (200, {}, json.dumps(
                {"historyId": self.newest, "history": recs}).encode())
        if "/messages/" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            if mid in self.fail_once and mid not in self._failed:
                self._failed.add(mid)
                return (429, {"Retry-After": "30"}, b"{}")
            if mid in self.gone:
                return (404, {}, b"{}")
            if mid in self.msg_status:
                return (self.msg_status[mid], {}, b"{}")
            if mid in self.poison:
                return (200, {}, json.dumps(
                    {"id": mid, "payload": "NOT-A-DICT"}).encode())
            return (200, {}, json.dumps(_msg(
                mid, {"From": f"{mid}@x.com", "Subject": mid})).encode())


class CursorDisciplineTests(HomeTestCase):
    def _seed(self, start="0"):
        self.seed_token(expiry_epoch=10**9)
        c = gm.GmailConnector()
        c.save_cursor({"history_id": start, "email": ACCT})

    def _emitted_ids(self):
        ids = set()
        for d in (self.home / ".assistant/inbox").glob("evt-gmail-*.json"):
            ids.add(json.loads(d.read_text())["external_id"])
        return ids

    def test_e1_burst_over_cap_loses_nothing_across_polls(self):
        # 250-message burst, cap 200 → first poll emits 200 and parks the
        # cursor at the last DELIVERED record (never the mailbox head); the
        # remaining 50 arrive next poll. Zero loss across polls.
        recs = [(i, f"m{i}") for i in range(1, 251)]
        self._seed("0")
        http = HistoryHttp(recs)
        c1 = gm.GmailConnector(http=http, oauth_transport=lambda u, f: {})
        c1.config["max_events_per_poll"] = 200
        r1 = c1.poll_once(now=1)
        self.assertEqual(r1["emitted"], 200)
        self.assertTrue(r1["truncated"] or r1["emitted"] == 200)
        self.assertEqual(len(self._emitted_ids()), 200)
        # cursor parked at record 200, NOT the newest (250)
        self.assertEqual(c1.load_cursor()["history_id"], "200")

        c2 = gm.GmailConnector(http=http, oauth_transport=lambda u, f: {})
        c2.config["max_events_per_poll"] = 200
        r2 = c2.poll_once(now=2)
        self.assertEqual(r2["emitted"], 50)
        self.assertEqual(len(self._emitted_ids()), 250)  # all 250, no loss
        self.assertEqual(c2.load_cursor()["history_id"], "250")

    def test_e2_transient_per_message_failure_refetches_victim(self):
        # One 429 mid-batch must NOT advance the cursor past the victim; the
        # victim is re-fetched and delivered on the next poll.
        recs = [(1, "m1"), (2, "m2"), (3, "m3")]
        self._seed("0")
        http = HistoryHttp(recs, fail_once={"m2"})
        c1 = gm.GmailConnector(http=http, oauth_transport=lambda u, f: {})
        r1 = c1.poll_once(now=1)
        self.assertEqual(r1["emitted"], 1)              # only m1 got through
        self.assertEqual(self._emitted_ids(), {"gmail:m1"})
        self.assertEqual(c1.load_cursor()["history_id"], "1")  # parked at m1

        c2 = gm.GmailConnector(http=http, oauth_transport=lambda u, f: {})
        r2 = c2.poll_once(now=2)
        self.assertEqual(r2["emitted"], 2)              # m2 (retried) + m3
        self.assertEqual(self._emitted_ids(),
                         {"gmail:m1", "gmail:m2", "gmail:m3"})
        self.assertEqual(c2.load_cursor()["history_id"], "3")

    def test_e4_poison_item_is_skipped_counted_and_does_not_wedge(self):
        # [good, poison, good] → both goods delivered, poison counted in the
        # heartbeat (not silent), cursor advances (no wedge behind the poison).
        recs = [(1, "m1"), (2, "bad"), (3, "m3")]
        self._seed("0")
        http = HistoryHttp(recs, poison={"bad"})
        c = gm.GmailConnector(http=http, oauth_transport=lambda u, f: {})
        r = c.poll_once(now=1)
        self.assertEqual(r["emitted"], 2)
        self.assertEqual(r["malformed"], 1)
        self.assertEqual(self._emitted_ids(), {"gmail:m1", "gmail:m3"})
        self.assertEqual(c.load_cursor()["history_id"], "3")  # not wedged
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertTrue(any("malformed" in e for e in hb["errors"]))

    def test_e5_history_404_reseed_is_visible_in_heartbeat(self):
        # The 404-reseed is a real loss window — it must degrade ok/show the
        # error, not report ok:true/errors:[] like a clean seed.
        self.seed_token(expiry_epoch=10**9)
        c0 = self.make(RouterHttp(profile=(
            200, {}, json.dumps({"historyId": "9",
                                 "emailAddress": ACCT}).encode())))
        c0.poll_once(now=1)
        c = self.make(RouterHttp(
            history=(404, {}, b"{}"),
            profile=(200, {}, json.dumps({"historyId": "77",
                                          "emailAddress": ACCT}).encode())))
        res = c.poll_once(now=2)
        self.assertEqual(res["status"], "seeded")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])            # E5: NOT ok:true
        self.assertTrue(any("404" in e for e in hb["errors"]))

    def test_o1_oauth_failure_heartbeat_carries_cached_expiry(self):
        # A wrapped refresh failure (network-down) must leave the token-expiry
        # signal intact: the fallback heartbeat carries the CACHED expiry, not
        # null. (The default transport wraps URLError as OAuthError — see the
        # base test; here we assert the heartbeat side.)
        self.seed_token(expiry_epoch=1000)

        def bad_oauth(uri, form):
            raise gm.connector.OAuthError("token endpoint unreachable")

        c = gm.GmailConnector(http=RouterHttp(), oauth_transport=bad_oauth)
        c.poll_once(now=1783080000)
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["token_expiry_epoch"], 1000)  # cached, not null
        self.assertFalse(hb["ok"])

    def test_o1_once_writes_heartbeat_on_failure_not_crash(self):
        # `--once` with no token cache must exit cleanly WITH a heartbeat
        # (unhandled traceback + no heartbeat was the O1 hazard under launchd).
        rc = gm.main(["--once"])
        self.assertEqual(rc, 0)
        hb_path = (self.home / ".assistant/connectors/gmail/heartbeat.json")
        self.assertTrue(hb_path.exists())
        hb = json.loads(hb_path.read_text())
        self.assertFalse(hb["ok"])


if __name__ == "__main__":
    unittest.main()
