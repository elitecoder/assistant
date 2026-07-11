"""Tests for bin/connectors/outlook.py — the M5 wave-3 Outlook (readonly) mail
connector, against injected HTTP + OAuth transports (no network).

Proves: mechanical kind derivation from Microsoft Graph metadata with
DIRECT-ADDRESSED classified BEFORE any newsletter signal (SEC2); the delta
cursor discipline (seed emits nothing; incremental advances the deltaLink only
after a full page set is dropped; a truncation PARKS the nextLink; a 410 Gone
full-resyncs and SURFACES the loss in the heartbeat); per-item poison skip; and
GET-only read-only discipline. Plus the M5 acceptance fixtures: double-delivery
→ one event; kill-mid-batch → no loss / no dupes; token absent → not_configured.

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

from assistant import eventspine  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ol = _load("ol_conn_test", "bin/connectors/outlook.py")
GRAPH = ol.GRAPH_BASE
ACCT = "me@contoso.com"


def _rcpt(*addrs):
    return [{"emailAddress": {"address": a}} for a in addrs]


def _msg(mid, *, to=None, cc=None, inf="focused", frm="s@corp.com",
         name="Sender", subject=None, preview="preview", received=None,
         is_read=False):
    return {
        "id": mid,
        "subject": subject if subject is not None else f"Subject {mid}",
        "bodyPreview": preview,
        "webLink": f"https://outlook.office365.com/mail/inbox/id/{mid}",
        "receivedDateTime": received or "2026-07-10T12:00:00Z",
        "isRead": is_read,
        "inferenceClassification": inf,
        "from": {"emailAddress": {"name": name, "address": frm}},
        "toRecipients": _rcpt(*(to or [])),
        "ccRecipients": _rcpt(*(cc or [])),
    }


def _page(items, *, delta=None, nxt=None):
    d = {"value": items}
    if nxt:
        d["@odata.nextLink"] = nxt
    if delta:
        d["@odata.deltaLink"] = delta
    return (200, {}, json.dumps(d).encode())


class RouterHttp:
    """Substring-routed fake transport. Records calls; asserts GET-only."""
    def __init__(self, routes):
        self.routes = routes           # list of (substr, response)
        self.calls = []
        self.headers = []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url))
        self.headers.append(headers or {})
        assert method == "GET", f"connector must be read-only, got {method}"
        for sub, resp in self.routes:
            if sub in url:
                return resp
        raise AssertionError("no route for " + url)


# ─── kind classification (pure; SEC2 direct-first) ────────────────────────────

class ClassificationTests(unittest.TestCase):
    def _kind(self, msg, acct=ACCT):
        return ol.message_to_event(msg, acct)["kind"]

    def test_direct_when_addressed_in_to(self):
        self.assertEqual(self._kind(_msg("m", to=[ACCT])), "direct")

    def test_direct_wins_over_newsletter_signal(self):
        # SEC2: a message addressed straight to the owner is NEVER auto-dropped
        # as newsletter — direct is decided BEFORE the inference=="other" signal.
        self.assertEqual(self._kind(_msg("m", to=[ACCT], inf="other")), "direct")

    def test_direct_case_insensitive(self):
        self.assertEqual(self._kind(_msg("m", to=["ME@Contoso.COM"])), "direct")

    def test_cc_when_only_in_cc(self):
        self.assertEqual(
            self._kind(_msg("m", to=["team@corp.com"], cc=[ACCT])), "cc")

    def test_cc_wins_over_newsletter_signal(self):
        self.assertEqual(
            self._kind(_msg("m", to=["team@corp.com"], cc=[ACCT], inf="other")),
            "cc")

    def test_newsletter_when_inference_other_and_not_addressed(self):
        self.assertEqual(
            self._kind(_msg("m", to=["list@news.example"], inf="other")),
            "newsletter")

    def test_message_when_focused_and_not_addressed(self):
        self.assertEqual(self._kind(_msg("m", to=["dl@corp.com"])), "message")

    def test_alias_in_owner_set_is_direct_not_newsletter(self):
        # D1: the owner's FULL address set (mail + proxyAddresses aliases) is
        # passed in; a message to an ALIAS is direct — never mis-laned as a
        # droppable newsletter — even with the inference=="other" signal.
        owner = {"me@contoso.com", "alias@contoso.com"}
        self.assertEqual(
            ol.message_to_event(_msg("m", to=["alias@contoso.com"], inf="other"),
                                owner)["kind"], "direct")

    def test_alias_in_owner_set_cc_is_cc(self):
        owner = {"me@contoso.com", "alias@contoso.com"}
        self.assertEqual(
            ol.message_to_event(_msg("m", to=["team@corp.com"],
                                     cc=["alias@contoso.com"]), owner)["kind"],
            "cc")

    def test_unknown_account_never_newsletter(self):
        # Without the account address we cannot confirm a message is NOT direct,
        # so an inference=="other" mail degrades to `message` (never auto-drop).
        self.assertEqual(self._kind(_msg("m", to=["x@corp.com"], inf="other"),
                                    acct=""), "message")

    def test_event_shape_external_id_actor_refs_ts(self):
        ev = ol.message_to_event(
            _msg("AAMkMSG1", to=[ACCT], frm="boss@corp.com", name="The Boss",
                 subject="Hello", preview="hi there"), ACCT)
        self.assertEqual(ev["schema"], "world-event/1")
        self.assertEqual(ev["source"], "outlook")
        self.assertEqual(ev["external_id"], "outlook:AAMkMSG1")
        self.assertEqual(ev["actor"], "The Boss <boss@corp.com>")
        self.assertEqual(ev["refs"], {"sender": "boss@corp.com"})
        self.assertEqual(ev["url"],
                         "https://outlook.office365.com/mail/inbox/id/AAMkMSG1")
        self.assertEqual(ev["ts"], "2026-07-10T12:00:00Z")
        self.assertEqual(ev["snippet"], "hi there")

    def test_missing_subject_and_from(self):
        ev = ol.message_to_event({"id": "x", "from": {},
                                  "toRecipients": _rcpt("dl@corp.com")}, ACCT)
        self.assertEqual(ev["title"], "(no subject)")
        self.assertEqual(ev["actor"], "")
        self.assertEqual(ev["refs"], {})


# ─── delta cursor discipline (injected transport) ─────────────────────────────

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

    def seed_token(self):
        c = ol.OutlookConnector()
        p = c.token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "access_token": "A", "refresh_token": "R",
            "expiry_epoch": 9999999999, "client_id": "c",
            "client_secret": "s", "token_uri": ol.connector.MICROSOFT_TOKEN_URI}))

    def make(self, http, cursor=None):
        self.seed_token()
        c = ol.OutlookConnector(
            http=http, oauth_transport=lambda u, f: {"access_token": "A",
                                                     "expires_in": 3600})
        if cursor is not None:
            c.save_cursor(cursor)
        return c

    def drops(self):
        return sorted((self.home / ".assistant/inbox").glob("evt-*.json"))

    def events(self):
        return [json.loads(p.read_text()) for p in self.drops()]


class DeltaCursorTests(HomeTestCase):
    def test_seed_emits_nothing_and_stores_deltalink(self):
        me = (200, {}, json.dumps({"mail": ACCT}).encode())
        seed = _page([_msg("s1", to=[ACCT])],
                     delta=GRAPH + "/delta?$deltatoken=D1")
        c = self.make(RouterHttp([("/me?", me),
                                  ("/messages/delta", seed),
                                  ("$deltatoken=D1", seed)]))
        res = c.poll_once(now=1000)
        self.assertEqual(res["mode"], "seed")
        self.assertEqual(res["emitted"], 0)          # never dump the mailbox
        self.assertEqual(len(self.drops()), 0)
        cur = c.load_cursor()
        self.assertIn("D1", cur["delta_link"])
        self.assertEqual(cur["email"], ACCT)

    def test_incremental_emits_and_advances_deltalink(self):
        page = _page([_msg("d1", to=[ACCT]), _msg("n1", inf="other")],
                     delta=GRAPH + "/delta?$deltatoken=D2")
        c = self.make(RouterHttp([("$deltatoken=D1", page)]),
                      cursor={"delta_link": GRAPH + "/delta?$deltatoken=D1",
                              "email": ACCT})
        res = c.poll_once(now=1001)
        self.assertEqual(res["mode"], "incremental")
        self.assertEqual(res["emitted"], 2)
        kinds = {e["external_id"]: e["kind"] for e in self.events()}
        self.assertEqual(kinds["outlook:d1"], "direct")
        self.assertEqual(kinds["outlook:n1"], "newsletter")
        self.assertIn("D2", c.load_cursor()["delta_link"])

    def test_truncation_parks_nextlink_without_advancing(self):
        c = self.make(None, cursor={"delta_link": GRAPH + "/delta?$deltatoken=D0",
                                    "email": ACCT})
        c.config = dict(c.config)
        c.config["max_events_per_poll"] = 1          # force a page-boundary cap
        pg1 = _page([_msg("t1", to=[ACCT])], nxt=GRAPH + "/delta?$skiptoken=S2")
        pg2 = _page([_msg("t2", to=[ACCT])],
                    delta=GRAPH + "/delta?$deltatoken=D9")
        c._http = RouterHttp([("$deltatoken=D0", pg1), ("$skiptoken=S2", pg2)])
        res = c.poll_once(now=1002)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["emitted"], 1)
        cur = c.load_cursor()
        self.assertIn("S2", cur["resume_link"])      # parked at page boundary
        self.assertIsNone(cur.get("delta_link"))     # NOT advanced past unfetched
        # Next poll resumes and completes to the fresh deltaLink.
        c._http = RouterHttp([("$skiptoken=S2", pg2)])
        res2 = c.poll_once(now=1003)
        self.assertEqual(res2["emitted"], 1)
        cur2 = c.load_cursor()
        self.assertIn("D9", cur2["delta_link"])
        self.assertIsNone(cur2.get("resume_link"))
        self.assertEqual({e["external_id"] for e in self.events()},
                         {"outlook:t1", "outlook:t2"})

    def test_410_full_resync_surfaced_in_heartbeat(self):
        me = (200, {}, json.dumps({"mail": ACCT}).encode())
        gone = (410, {}, b"{}")
        reseed = _page([_msg("z1", to=[ACCT])],
                       delta=GRAPH + "/delta?$deltatoken=D3")
        c = self.make(RouterHttp([("$deltatoken=D2", gone),
                                  ("/me?", me),
                                  ("/messages/delta", reseed),
                                  ("$deltatoken=D3", reseed)]),
                      cursor={"delta_link": GRAPH + "/delta?$deltatoken=D2",
                              "email": ACCT})
        res = c.poll_once(now=1004)
        self.assertEqual(res["status"], "ok")        # poll completed (resynced)
        self.assertEqual(res["emitted"], 0)          # reseed emits nothing
        self.assertEqual(len(self.drops()), 0)
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])                   # loss window is NOT healthy
        self.assertTrue(any("410" in e for e in hb["errors"]))
        self.assertIn("D3", c.load_cursor()["delta_link"])

    def test_poison_item_skipped_others_emitted(self):
        # A malformed item (from is a string, not an object → normalization
        # raises) is skip-and-counted; the good item is STILL emitted (the batch
        # is not wedged) and the cursor advances cleanly.
        bad = {"id": "bad1", "from": "not-an-object",
               "receivedDateTime": 123}
        page = _page([bad, _msg("ok1", to=[ACCT])],
                     delta=GRAPH + "/delta?$deltatoken=DP")
        c = self.make(RouterHttp([("$deltatoken=DI", page)]),
                      cursor={"delta_link": GRAPH + "/delta?$deltatoken=DI",
                              "email": ACCT})
        res = c.poll_once(now=1005)
        ids = {e["external_id"] for e in self.events()}
        self.assertEqual(ids, {"outlook:ok1"})       # good item delivered
        self.assertEqual(res["emitted"], 1)
        self.assertGreaterEqual(res["malformed"], 1)  # poison counted, not silent
        self.assertIn("DP", c.load_cursor()["delta_link"])
        self.assertEqual(res["status"], "ok")

    def test_removed_tombstone_is_skipped(self):
        page = _page([{"@removed": {"reason": "deleted"}, "id": "gone1"},
                      _msg("live1", to=[ACCT])],
                     delta=GRAPH + "/delta?$deltatoken=DR")
        c = self.make(RouterHttp([("$deltatoken=DX", page)]),
                      cursor={"delta_link": GRAPH + "/delta?$deltatoken=DX",
                              "email": ACCT})
        res = c.poll_once(now=1006)
        self.assertEqual(res["emitted"], 1)
        self.assertEqual(res["removed"], 1)
        self.assertEqual({e["external_id"] for e in self.events()},
                         {"outlook:live1"})

    def test_all_graph_calls_are_get(self):
        me = (200, {}, json.dumps({"mail": ACCT}).encode())
        seed = _page([], delta=GRAPH + "/delta?$deltatoken=D1")
        http = RouterHttp([("/me?", me), ("/messages/delta", seed),
                           ("$deltatoken=D1", seed)])
        c = self.make(http)
        c.poll_once(now=1007)
        self.assertTrue(http.calls)
        for method, _url in http.calls:
            self.assertEqual(method, "GET")

    def test_me_fetch_requests_proxyaddresses_and_stores_owner_set(self):
        # D2: /me is fetched (needs the User.Read scope) with proxyAddresses in
        # $select; D1: the owner's full alias set is stored on the cursor.
        me = (200, {}, json.dumps({
            "mail": "me@contoso.com",
            "userPrincipalName": "me@contoso.onmicrosoft.com",
            "proxyAddresses": ["SMTP:me@contoso.com", "smtp:alias@contoso.com",
                               "sip:me@contoso.com"]}).encode())
        seed = _page([], delta=GRAPH + "/delta?$deltatoken=DD")
        http = RouterHttp([("/me?", me), ("/messages/delta", seed),
                           ("$deltatoken=DD", seed)])
        c = self.make(http)
        c.poll_once(now=1)
        self.assertTrue(any("proxyAddresses" in u for _m, u in http.calls),
                        "/me must request proxyAddresses")
        cur = c.load_cursor()
        self.assertEqual(cur["email"], "me@contoso.com")
        self.assertEqual(set(cur["addrs"]),
                         {"me@contoso.com", "me@contoso.onmicrosoft.com",
                          "alias@contoso.com"})   # smtp: aliases only; sip: dropped

    def test_alias_addressed_mail_classifies_direct_end_to_end(self):
        # D1 end-to-end: a message to an ALIAS (from proxyAddresses) with
        # inference=="other" is emitted as `direct`, NOT a droppable newsletter.
        me = (200, {}, json.dumps({
            "mail": "me@contoso.com",
            "proxyAddresses": ["SMTP:me@contoso.com",
                               "smtp:alias@contoso.com"]}).encode())
        page = _page([_msg("a1", to=["alias@contoso.com"], inf="other")],
                     delta=GRAPH + "/delta?$deltatoken=DA2")
        c = self.make(RouterHttp([("/me?", me), ("$deltatoken=DA1", page)]),
                      cursor={"delta_link": GRAPH + "/delta?$deltatoken=DA1"})
        c.poll_once(now=1)
        ev = {e["external_id"]: e for e in self.events()}
        self.assertEqual(ev["outlook:a1"]["kind"], "direct")

    def test_immutable_id_header_sent_on_every_graph_request(self):
        # D6: Prefer: IdType="ImmutableId" makes ids survive folder moves.
        me = (200, {}, json.dumps({"mail": ACCT}).encode())
        seed = _page([], delta=GRAPH + "/delta?$deltatoken=DH")
        http = RouterHttp([("/me?", me), ("/messages/delta", seed),
                           ("$deltatoken=DH", seed)])
        c = self.make(http)
        c.poll_once(now=1)
        self.assertTrue(http.headers)
        for h in http.headers:
            self.assertEqual(h.get("Prefer"), 'IdType="ImmutableId"')

    def test_persistent_410_is_bounded_one_reseed_no_recursion(self):
        # D4: a persistent 410 (mailbox migration) must NOT recurse ~1000 GETs
        # into a RecursionError. It reseeds AT MOST once, then surfaces an error
        # + heartbeat and backs off. Exactly two GETs: the original + one reseed.
        gone = (410, {}, b"{}")
        http = RouterHttp([("delta", gone)])
        c = self.make(http, cursor={
            "delta_link": GRAPH + "/delta?$deltatoken=DG", "email": ACCT})
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "status_410")
        self.assertEqual(len(http.calls), 2)         # bounded — NOT ~1000
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertTrue(any("410" in e for e in hb["errors"]))

    def test_dry_run_is_side_effect_free(self):
        # A dry run must not drop, advance the cursor, or write a heartbeat.
        self.seed_token()
        page = _page([_msg("d1", to=[ACCT])],
                     delta=GRAPH + "/delta?$deltatoken=D2")
        http = RouterHttp([("$deltatoken=D1", page)])
        c = ol.OutlookConnector(
            http=http, dry_run=True,
            oauth_transport=lambda u, f: {"access_token": "A",
                                          "expires_in": 3600})
        # pre-seed cursor via a NON-dry connector so there is a starting point
        c2 = ol.OutlookConnector()
        c2.save_cursor({"delta_link": GRAPH + "/delta?$deltatoken=D1",
                        "email": ACCT})
        c.poll_once(now=1008)
        self.assertEqual(len(self.drops()), 0)                 # no drop
        self.assertIn("D1", c.load_cursor()["delta_link"])     # cursor unmoved
        self.assertFalse(c.heartbeat_path().exists())          # no heartbeat


# ─── M5 acceptance fixtures ───────────────────────────────────────────────────

class DoubleDeliveryTests(HomeTestCase):
    def test_double_delivery_yields_one_event(self):
        # A self-referential deltaLink returns the SAME message on every poll —
        # a genuine double-delivery. The at-least-once producer drops twice; the
        # spine collapses to exactly ONE event → exactly one decision.
        self_delta = GRAPH + "/delta?$deltatoken=SELF"
        page = _page([_msg("dup1", to=[ACCT])], delta=self_delta)
        c = self.make(RouterHttp([("$deltatoken=SELF", page)]),
                      cursor={"delta_link": self_delta, "email": ACCT})
        c.poll_once(now=1)
        c.poll_once(now=2)
        self.assertEqual(len(self.drops()), 2)                 # 2 drops
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 1)           # 1 event
        self.assertEqual(summ["inbox_duplicates"], 1)


class KillMidBatchTests(HomeTestCase):
    def test_kill_before_cursor_save_loses_nothing_dupes_nothing(self):
        delta = GRAPH + "/delta?$deltatoken=K"
        page = _page([_msg("k1", to=[ACCT]), _msg("k2", to=[ACCT]),
                      _msg("k3", to=[ACCT])], delta=delta)
        c = self.make(RouterHttp([("$deltatoken=K", page)]),
                      cursor={"delta_link": delta, "email": ACCT})

        orig = c.save_cursor

        def boom(cursor):
            raise OSError("killed mid-batch")

        c.save_cursor = boom  # type: ignore
        with self.assertRaises(OSError):
            c.poll_once(now=1)
        # cursor watermark never advanced (still at K)
        self.assertIn("K", c.load_cursor()["delta_link"])

        # Restart: fresh connector, same durable state, re-fetches + re-emits.
        c2 = self.make(RouterHttp([("$deltatoken=K", page)]),
                       cursor=None)      # keep the on-disk cursor (K)
        c2.poll_once(now=2)
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 3)           # no loss
        ids = {json.loads(l)["external_id"]
               for l in eventspine.events_path().read_text().splitlines() if l}
        self.assertEqual(len(ids), 3)                          # no dupes


if __name__ == "__main__":
    unittest.main()
