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


if __name__ == "__main__":
    unittest.main()
