"""Tests for bin/connectors/jira.py — the M5 wave-2 JIRA connector (REWORKED).

The connector was rebuilt from "one event per issue snapshot" to a DELTA model,
and these tests assert that model against an injected credential provider + HTTP
transport (no network):

  * events come from DELTA objects — one per changelog history
    (jira:<key>:changelog:<id>) and one per comment (jira:<key>:comment:<id>),
    never the issue snapshot;
  * the field-change-then-comment REGRESSION → TWO spine events, never one
    swallowed (the verifier's repro);
  * mention detection by accountId — v2 [~accountid:...] markup AND v3 ADF
    mention nodes — not email substrings;
  * the epoch-MILLISECONDS watermark + `updated >= <ms>` JQL (TZ-safe), the
    currentUser() default scope, and $JIRA_JQL override;
  * nextPageToken pagination (no startAt) with keyset watermark parking;
  * the https pin (an http:// base is rejected before the PAT is sent);
  * the wave-1 invariants carried forward — contiguous-prefix watermark
    discipline, poison skip, double-delivery→one-event, kill-mid-batch, dry-run
    side-effect-freeness (incl. NO heartbeat write), GET-only read-only;
  * the >=15 rebuilt golden raw→event replay fixtures.

New module content (same filename), unittest style, tmp $HOME per test.
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


jira = _load("jira_test", "bin/connectors/jira.py")
JIRA_FIX = REPO / "evals" / "connectors" / "jira" / "fixtures"
BASE = "https://acme.atlassian.net"
ACCT_ID = "5b10a2844c20165700ede21g"
OTHER_ID = "557058:aa11bb22-cc33-dd44-ee55"
CREDS = (BASE, "mukul@corp.example", "TOK")


def _author(aid=OTHER_ID, name="Dana"):
    return {"accountId": aid, "displayName": name}


def _hist(hid, created, items, aid=OTHER_ID):
    return {"id": str(hid), "author": _author(aid), "created": created,
            "items": items}


def _comment(cid, created, body, updated=None, aid=OTHER_ID):
    return {"id": str(cid), "author": _author(aid), "body": body,
            "created": created, "updated": updated or created}


def _issue(key, updated, *, histories=None, comments=None, status="In Progress"):
    fields = {"summary": f"{key} summary", "updated": updated,
              "status": {"name": status},
              "assignee": {"accountId": ACCT_ID, "displayName": "Mukul"},
              "reporter": _author(name="Rep"), "priority": {"name": "Medium"}}
    if comments is not None:
        fields["comment"] = {"comments": comments}
    return {"key": key, "fields": fields,
            "changelog": {"histories": histories or []}}


TS = "2026-07-10T12:{:02d}:00.000+0000"


class SearchHttp:
    """A fake JIRA transport, GET-only. Answers /myself with the owner accountId
    and /rest/api/3/search/jql with a token-paged issue list. `pages` is a list of
    (issues, nextPageToken) tuples consumed in order across polls/pagination."""
    def __init__(self, issues=None, pages=None, account_id=ACCT_ID, tz="UTC"):
        self.single = issues
        self.pages = pages
        self.account_id = account_id
        self.tz = tz
        self.calls = []
        self._page_idx = 0

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url))
        assert method == "GET", f"read-only violated: {method}"
        if url.endswith(jira.MYSELF_PATH):
            return (200, {}, json.dumps(
                {"accountId": self.account_id, "emailAddress": "mukul@corp.example",
                 "timeZone": self.tz, "displayName": "Mukul"}).encode())
        assert jira.SEARCH_PATH in url, f"unexpected url {url}"
        if self.pages is not None:
            issues, nxt = self.pages[min(self._page_idx, len(self.pages) - 1)]
            self._page_idx += 1
            body = {"issues": issues, "isLast": not nxt}
            if nxt:
                body["nextPageToken"] = nxt
            return (200, {}, json.dumps(body).encode())
        return (200, {}, json.dumps(
            {"issues": self.single or [], "isLast": True}).encode())


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

    def _drops(self):
        return sorted((self.home / ".assistant/inbox").glob("evt-jira-*.json"))

    def _ids(self):
        return {json.loads(d.read_text())["external_id"] for d in self._drops()}

    def _kinds(self):
        return sorted(json.loads(d.read_text())["kind"] for d in self._drops())


def _make(http, creds=None, jql_scope="", account_id=None, **kw):
    return jira.JiraConnector(
        credentials_provider=lambda: (creds or CREDS),
        http=http, jql_scope=jql_scope, account_id=account_id, **kw)


# ─── kind derivation from DELTA objects ──────────────────────────────────────

class DeltaKindTests(unittest.TestCase):
    def test_changelog_status(self):
        h = _hist(1, TS.format(0), [{"field": "status", "toString": "Done"}])
        ev = jira.changelog_to_event(_issue("K-1", TS.format(0), histories=[h]), h)
        self.assertEqual(ev["kind"], "status_change")
        self.assertEqual(ev["external_id"], "jira:K-1:changelog:1")

    def test_changelog_assigned_to_me_by_accountid(self):
        h = _hist(2, TS.format(0), [{"field": "assignee", "to": ACCT_ID,
                                     "toString": "Mukul"}])
        ev = jira.changelog_to_event(_issue("K-2", TS.format(0)), h, ACCT_ID)
        self.assertEqual(ev["kind"], "assigned")

    def test_changelog_flagged(self):
        h = _hist(3, TS.format(0), [{"field": "Flagged", "toString": "Impediment"}])
        self.assertEqual(jira.changelog_to_event(
            _issue("K-3", TS.format(0)), h)["kind"], "flagged")

    def test_comment_plain_is_comment(self):
        c = _comment(9, TS.format(0), "just a note")
        ev = jira.comment_to_event(_issue("K-4", TS.format(0)), c, ACCT_ID)
        self.assertEqual(ev["kind"], "comment")
        self.assertEqual(ev["external_id"], "jira:K-4:comment:9")

    def test_comment_v2_markup_mention(self):
        c = _comment(10, TS.format(0), f"cc [~accountid:{ACCT_ID}] please look")
        self.assertEqual(jira.comment_to_event(
            _issue("K-5", TS.format(0)), c, ACCT_ID)["kind"], "mention")

    def test_comment_v3_adf_mention(self):
        body = {"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": ACCT_ID}},
            {"type": "text", "text": " ping"}]}]}
        c = _comment(11, TS.format(0), body)
        self.assertEqual(jira.comment_to_event(
            _issue("K-6", TS.format(0)), c, ACCT_ID)["kind"], "mention")

    def test_mention_of_someone_else_is_not_a_mention(self):
        c = _comment(12, TS.format(0), f"cc [~accountid:{OTHER_ID}] fyi")
        self.assertEqual(jira.comment_to_event(
            _issue("K-7", TS.format(0)), c, ACCT_ID)["kind"], "comment")

    def test_email_substring_no_longer_matches(self):
        # The dead email-substring path must NOT fire — only accountId does.
        c = _comment(13, TS.format(0), "cc mukul@corp.example please review")
        self.assertEqual(jira.comment_to_event(
            _issue("K-8", TS.format(0)), c, ACCT_ID)["kind"], "comment")


# ─── JQL: epoch-ms watermark, currentUser scope, override, https pin ─────────

class JqlTests(unittest.TestCase):
    def test_default_scope_and_epoch_ms_clause(self):
        jql = jira.build_jql(1_700_000_000_000)
        self.assertIn("updated >= 1700000000000", jql)
        self.assertIn("currentUser()", jql)
        self.assertTrue(jql.endswith("ORDER BY updated ASC"))

    def test_first_run_uses_relative_window(self):
        self.assertIn("updated >= -30d", jira.build_jql(0))

    def test_jira_jql_overrides_scope(self):
        jql = jira.build_jql(5, scope="project = OPS")
        self.assertIn("(project = OPS)", jql)
        self.assertNotIn("currentUser()", jql)

    def test_epoch_ms_is_tz_safe(self):
        # A Pacific-offset stamp and the same instant in UTC map to one ms value.
        a = jira._epoch_ms("2026-07-10T05:00:00.000-0700")
        b = jira._epoch_ms("2026-07-10T12:00:00.000+0000")
        self.assertEqual(a, b)


class HttpsPinTests(HomeTestCase):
    def test_http_base_is_rejected_before_sending_pat(self):
        http = SearchHttp([])
        c = _make(http, creds=("http://acme.atlassian.net", "e", "TOK"))
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "config_error")
        self.assertTrue(any("https" in e for e in res["errors"]))
        self.assertEqual(http.calls, [])           # no request ever went out
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])

    def test_credentials_provider_rejects_http(self):
        for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            os.environ.pop(k, None)
        os.environ["JIRA_BASE_URL"] = "http://insecure.example"
        os.environ["JIRA_API_TOKEN"] = "TOK"
        try:
            with self.assertRaises(ValueError):
                jira.jira_credentials()
        finally:
            for k in ("JIRA_BASE_URL", "JIRA_API_TOKEN"):
                os.environ.pop(k, None)


# ─── not_configured (PAT absent) ─────────────────────────────────────────────

class NotConfiguredTests(HomeTestCase):
    def test_absent_pat_is_quiet_not_configured(self):
        def no_creds():
            raise RuntimeError("JIRA not configured")
        c = jira.JiraConnector(credentials_provider=no_creds, http=SearchHttp([]))
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "not_configured")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["status"], "not_configured")
        self.assertTrue(hb["ok"])
        self.assertEqual(hb["errors"], [])


# ─── the delta model + the field-change-then-comment REGRESSION ──────────────

class DeltaModelTests(HomeTestCase):
    def test_field_change_then_comment_yields_two_events(self):
        # Poll 1: a status change (changelog history), no comment.
        p1 = _issue("R-1", TS.format(5),
                    histories=[_hist(500, TS.format(5),
                                     [{"field": "status", "toString": "Done"}])],
                    comments=[])
        _make(SearchHttp([p1])).poll_once(now=1)
        # Poll 2: SAME issue, `updated` bumped, a comment added, NO new changelog
        # history (the real-JIRA shape the old model swallowed).
        p2 = _issue("R-1", TS.format(6),
                    histories=[_hist(500, TS.format(5),
                                     [{"field": "status", "toString": "Done"}])],
                    comments=[_comment(600, TS.format(6),
                                       f"cc [~accountid:{ACCT_ID}] verify")])
        _make(SearchHttp([p2])).poll_once(now=2)
        # Two DISTINCT external_ids reach the spine — nothing swallowed.
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 2)
        ids = {json.loads(l)["external_id"]
               for l in eventspine.events_path().read_text().splitlines() if l}
        self.assertEqual(ids, {"jira:R-1:changelog:500", "jira:R-1:comment:600"})

    def test_multiple_histories_not_coalesced(self):
        iss = _issue("R-2", TS.format(8), histories=[
            _hist(1, TS.format(6), [{"field": "assignee", "to": ACCT_ID}]),
            _hist(2, TS.format(7), [{"field": "status", "toString": "Done"}]),
            _hist(3, TS.format(8), [{"field": "priority", "toString": "High"}])])
        r = _make(SearchHttp([iss])).poll_once(now=1)
        self.assertEqual(r["emitted"], 3)
        self.assertEqual(self._kinds(),
                         ["assigned", "priority_change", "status_change"])

    def test_comment_lane_fires_independently(self):
        iss = _issue("R-3", TS.format(9), histories=[], comments=[
            _comment(700, TS.format(9), f"[~accountid:{ACCT_ID}] urgent")])
        r = _make(SearchHttp([iss])).poll_once(now=1)
        self.assertEqual(r["emitted"], 1)
        self.assertEqual(self._ids(), {"jira:R-3:comment:700"})
        self.assertEqual(self._kinds(), ["mention"])


# ─── watermark cursor discipline (epoch-ms, contiguous prefix) ───────────────

class WatermarkDisciplineTests(HomeTestCase):
    def test_watermark_is_epoch_ms_of_last_emitted_issue(self):
        iss = _issue("A-1", TS.format(5), histories=[
            _hist(1, TS.format(5), [{"field": "status", "toString": "Done"}])])
        c = _make(SearchHttp([iss]))
        c.poll_once(now=1)
        self.assertEqual(c.load_cursor()["watermark_ms"],
                         jira._epoch_ms(TS.format(5)))

    def test_transient_failure_parks_watermark_before_failed_issue(self):
        issues = [
            _issue("A-1", TS.format(0), histories=[
                _hist(1, TS.format(0), [{"field": "status"}])]),
            _issue("A-2", TS.format(1), histories=[
                _hist(2, TS.format(1), [{"field": "status"}])]),
            _issue("A-3", TS.format(2), histories=[
                _hist(3, TS.format(2), [{"field": "status"}])]),
        ]
        c = _make(SearchHttp(issues))
        orig = c.emit
        n = {"i": 0}

        def femit(ev, raw=None, now=None):
            n["i"] += 1
            if n["i"] == 2:
                raise OSError("drop failed")
            return orig(ev, raw=raw, now=now)
        c.emit = femit  # type: ignore
        r1 = c.poll_once(now=1)
        self.assertEqual(r1["emitted"], 1)                     # only A-1
        self.assertEqual(self._ids(), {"jira:A-1:changelog:1"})
        self.assertEqual(c.load_cursor()["watermark_ms"],
                         jira._epoch_ms(TS.format(0)))          # parked at A-1

        # Restart clean: re-query from the parked ms; A-1 re-appears (>= overlap)
        # and dedups, A-2/A-3 now emit. No loss.
        c2 = _make(SearchHttp(issues))
        c2.poll_once(now=2)
        self.assertEqual(self._ids(), {"jira:A-1:changelog:1",
                                       "jira:A-2:changelog:2",
                                       "jira:A-3:changelog:3"})

    def test_poison_issue_skipped_counted_no_wedge(self):
        good = _issue("G-1", TS.format(0), histories=[
            _hist(1, TS.format(0), [{"field": "status"}])])
        poison = {"key": "BAD", "fields": "corrupt",  # str .get → AttributeError
                  "changelog": {"histories": []}}
        good2 = _issue("G-2", TS.format(1), histories=[
            _hist(2, TS.format(1), [{"field": "priority"}])])
        c = _make(SearchHttp([good, poison, good2]))
        r = c.poll_once(now=1)
        self.assertEqual(r["emitted"], 2)
        self.assertEqual(r["malformed"], 1)
        self.assertEqual(self._ids(), {"jira:G-1:changelog:1",
                                       "jira:G-2:changelog:2"})
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertTrue(any("malformed" in e for e in hb["errors"]))


# ─── pagination via nextPageToken (no startAt), keyset watermark ─────────────

class PaginationTests(HomeTestCase):
    def test_nextpagetoken_walks_all_pages(self):
        p1 = [_issue("P-1", TS.format(0), histories=[
            _hist(1, TS.format(0), [{"field": "status"}])])]
        p2 = [_issue("P-2", TS.format(1), histories=[
            _hist(2, TS.format(1), [{"field": "status"}])])]
        http = SearchHttp(pages=[(p1, "TKN2"), (p2, None)])
        r = _make(http).poll_once(now=1)
        self.assertEqual(r["emitted"], 2)
        self.assertEqual(self._ids(), {"jira:P-1:changelog:1",
                                       "jira:P-2:changelog:2"})
        # the SECOND search call carried the nextPageToken (no startAt anywhere)
        search_urls = [u for _, u in http.calls if jira.SEARCH_PATH in u]
        self.assertIn("nextPageToken=TKN2", search_urls[1])
        self.assertFalse(any("startAt" in u for u in search_urls))

    def test_truncation_parks_watermark_at_last_emitted(self):
        p1 = [_issue("P-1", TS.format(0), histories=[
            _hist(1, TS.format(0), [{"field": "status"}])])]
        # page 1 has a nextPageToken but we cap max_pages=1 → truncate.
        http = SearchHttp(pages=[(p1, "TKN2")])
        c = _make(http)
        c.config["max_pages"] = 1
        r = c.poll_once(now=1)
        self.assertTrue(r["truncated"])
        self.assertEqual(c.load_cursor()["watermark_ms"],
                         jira._epoch_ms(TS.format(0)))     # parked, not lost


# ─── acceptance: double-delivery → one event; kill-mid-batch ─────────────────

class AcceptanceTests(HomeTestCase):
    def test_double_delivery_yields_one_event(self):
        iss = [_issue("D-1", TS.format(0), histories=[
            _hist(1, TS.format(0), [{"field": "assignee", "to": ACCT_ID}])])]
        _make(SearchHttp(iss)).poll_once(now=1)
        _make(SearchHttp(iss)).poll_once(now=2)   # >= overlap re-delivers
        self.assertEqual(len(self._drops()), 2)   # at-least-once producer
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 1)
        self.assertEqual(summ["inbox_duplicates"], 1)

    def test_kill_before_cursor_save_loses_nothing(self):
        iss = [_issue("K-1", TS.format(0), histories=[
                   _hist(1, TS.format(0), [{"field": "status"}])]),
               _issue("K-2", TS.format(1), histories=[
                   _hist(2, TS.format(1), [{"field": "priority"}])])]
        c = _make(SearchHttp(iss))

        def boom(cursor):
            raise OSError("killed mid-batch")
        c.save_cursor = boom  # type: ignore
        with self.assertRaises(OSError):
            c.poll_once(now=1)
        self.assertEqual(c.load_cursor(), {})     # watermark never advanced

        c2 = _make(SearchHttp(iss))
        c2.poll_once(now=2)
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 2)


# ─── dry-run side-effect-freeness (incl. NO heartbeat write — finding 16) ────

class DryRunTests(HomeTestCase):
    def test_dry_run_writes_nothing_and_no_heartbeat(self):
        iss = [_issue("DR-1", TS.format(0), histories=[
            _hist(1, TS.format(0), [{"field": "status"}])])]
        c = _make(SearchHttp(iss), dry_run=True)
        c.poll_once(now=1)
        self.assertEqual(self._drops(), [])              # nothing dropped
        self.assertFalse(c.cursor_path().exists())       # cursor not saved
        self.assertFalse(c.heartbeat_path().exists())    # NO heartbeat (finding 16)


# ─── read-only guarantee ─────────────────────────────────────────────────────

class ReadOnlyTests(unittest.TestCase):
    def test_only_get_is_issued(self):
        http = SearchHttp([_issue("X-1", TS.format(0), histories=[
            _hist(1, TS.format(0), [{"field": "status"}])])])
        jira.JiraConnector(credentials_provider=lambda: CREDS,
                           http=http, jql_scope="").poll_once(now=1)
        self.assertTrue(all(m == "GET" for m, _ in http.calls))


# ─── golden raw→event replay (rebuilt, realistic Cloud payloads) ─────────────

class GoldenReplayTests(unittest.TestCase):
    def test_replay_delta_events(self):
        files = sorted(JIRA_FIX.glob("*.json"))
        self.assertGreaterEqual(len(files), 15,
                                msg=f"{JIRA_FIX} needs >=15 recorded events")
        for f in files:
            data = json.loads(f.read_text())
            got = jira.issue_deltas(data["raw"], data.get("account_id", ""),
                                    int(data.get("watermark_ms") or 0))
            got = [{k: v for k, v in ev.items()
                    if k not in ("id", "raw_path", "epoch")} for ev in got]
            self.assertEqual(got, data["expected"],
                             msg=f"normalization drift in {f.name}")

    def test_expected_are_wellformed_worldevents(self):
        for f in JIRA_FIX.glob("*.json"):
            for ev in json.loads(f.read_text())["expected"]:
                self.assertEqual(ev["schema"], "world-event/1")
                self.assertTrue(ev["external_id"].startswith("jira:"))
                self.assertEqual(ev["source"], "jira")

    def test_regression_fixture_encodes_two_events(self):
        data = json.loads(
            (JIRA_FIX / "10-regression-field-then-comment.json").read_text())
        kinds = [e["kind"] for e in data["expected"]]
        ext = [e["external_id"] for e in data["expected"]]
        self.assertEqual(len(data["expected"]), 2)
        self.assertTrue(any(":changelog:" in e for e in ext))
        self.assertTrue(any(":comment:" in e for e in ext))
        self.assertIn("mention", kinds)


if __name__ == "__main__":
    unittest.main()
