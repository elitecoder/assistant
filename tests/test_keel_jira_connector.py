"""Tests for bin/connectors/jira.py — the M5 wave-2 JIRA connector.

Proves, against an injected credential provider + HTTP transport (no network):
mechanical kind derivation from the changelog, the PAT-provider not_configured
path (mirrors github's gh_cli_token), the JQL builder (minute-floored ``>=`` so
same-minute siblings are never lost), the watermark cursor discipline (a
transient mid-batch failure parks the watermark at the last contiguous emitted
issue — the wave-1 blocker), poison skip, the double-delivery→one-event and
kill-mid-batch acceptance cases, GET-only read-only discipline, and the ≥15
golden raw→event replay fixtures.

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


jira = _load("jira_test", "bin/connectors/jira.py")
JIRA_FIX = REPO / "evals" / "connectors" / "jira" / "fixtures"
ACCT = "mukul@corp.example"
CREDS = ("https://acme.atlassian.net", ACCT, "TOK")


def _issue(key, updated, hid, items, comment=None, status="In Progress"):
    f = {"summary": f"{key} summary", "updated": updated,
         "status": {"name": status}, "assignee": {"displayName": "Mukul"},
         "reporter": {"displayName": "R"}, "priority": {"name": "Medium"}}
    if comment:
        f["comment"] = {"comments": [{"body": comment}]}
    return {"key": key, "fields": f,
            "changelog": {"histories": [{"id": str(hid), "items": items}]}}


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


class SearchHttp:
    """A fake JIRA search transport returning one page of issues, GET-only."""
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url))
        assert method == "GET", f"read-only violated: {method}"
        return (200, {}, json.dumps(
            {"issues": self.issues, "total": len(self.issues),
             "startAt": 0, "maxResults": 100}).encode())


def _make(http, account=ACCT, creds=None):
    return jira.JiraConnector(
        credentials_provider=lambda: (creds or CREDS),
        http=http, account=account, jql_extra="")


# ─── kind derivation ─────────────────────────────────────────────────────────

class KindTests(unittest.TestCase):
    def test_mention_wins(self):
        iss = _issue("K-1", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "comment"}], comment=f"[~{ACCT}] hi")
        self.assertEqual(jira.derive_kind(iss, ACCT), "mention")

    def test_flagged(self):
        iss = _issue("K-2", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "Flagged", "toString": "Impediment"}])
        self.assertEqual(jira.derive_kind(iss, ACCT), "flagged")

    def test_assigned_to_me(self):
        iss = _issue("K-3", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "assignee", "to": "a", "toString": ACCT}])
        self.assertEqual(jira.derive_kind(iss, ACCT), "assigned")

    def test_status_change(self):
        iss = _issue("K-4", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "status", "toString": "Done"}])
        self.assertEqual(jira.derive_kind(iss, ACCT), "status_change")

    def test_priority_change(self):
        iss = _issue("K-5", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "priority", "toString": "High"}])
        self.assertEqual(jira.derive_kind(iss, ACCT), "priority_change")

    def test_comment(self):
        iss = _issue("K-6", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "comment"}])
        self.assertEqual(jira.derive_kind(iss, ACCT), "comment")

    def test_updated_fallback(self):
        iss = _issue("K-7", "2026-07-10T12:00:00.000+0000", 1,
                     [{"field": "labels"}])
        self.assertEqual(jira.derive_kind(iss, ACCT), "updated")

    def test_external_id_key_and_changelog_id(self):
        iss = _issue("K-8", "2026-07-10T12:00:00.000+0000", 9099,
                     [{"field": "status"}])
        ev = jira.issue_to_event(iss, ACCT)
        self.assertEqual(ev["external_id"], "jira:K-8:9099")
        self.assertEqual(ev["refs"]["jira"], "K-8")

    def test_jql_uses_ge_minute_floor_and_order(self):
        self.assertEqual(
            jira.build_jql("2026-07-10 12:05"),
            'updated >= "2026-07-10 12:05" ORDER BY updated ASC')


# ─── not_configured (PAT absent) ─────────────────────────────────────────────

class NotConfiguredTests(HomeTestCase):
    def test_absent_pat_is_quiet_not_configured(self):
        def no_creds():
            raise RuntimeError("JIRA not configured")
        c = jira.JiraConnector(credentials_provider=no_creds,
                               http=SearchHttp([]))
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "not_configured")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["status"], "not_configured")
        self.assertTrue(hb["ok"])
        self.assertEqual(hb["errors"], [])

    def test_default_provider_raises_without_env(self):
        for k in ("JIRA_BASE_URL", "JIRA_API_TOKEN"):
            os.environ.pop(k, None)
        with self.assertRaises(RuntimeError):
            jira.jira_credentials()


# ─── watermark cursor discipline (the blocker) ──────────────────────────────

class WatermarkDisciplineTests(HomeTestCase):
    def test_transient_failure_parks_watermark_at_contiguous_prefix(self):
        issues = [
            _issue("A-1", "2026-07-10T12:00:00.000+0000", 100,
                   [{"field": "assignee", "to": "a", "toString": ACCT}]),
            _issue("A-2", "2026-07-10T12:01:00.000+0000", 101,
                   [{"field": "status", "toString": "Done"}]),
            _issue("A-3", "2026-07-10T12:02:00.000+0000", 102,
                   [{"field": "priority", "toString": "High"}]),
        ]
        c = _make(SearchHttp(issues))
        # Make the SECOND emit fail transiently.
        orig = c.emit
        n = {"i": 0}

        def femit(ev, raw=None, now=None):
            n["i"] += 1
            if n["i"] == 2:
                raise OSError("drop failed")
            return orig(ev, raw=raw, now=now)
        c.emit = femit  # type: ignore
        r1 = c.poll_once(now=1)
        self.assertEqual(r1["emitted"], 1)                 # only A-1
        self.assertEqual(self._ids(), {"jira:A-1:100"})
        self.assertEqual(c.load_cursor()["watermark"], "2026-07-10 12:00")

        # Restart clean: re-query from the parked minute; A-1 re-appears (>=
        # boundary) and dedups downstream, A-2/A-3 now emit. No loss.
        c2 = _make(SearchHttp(issues))
        r2 = c2.poll_once(now=2)
        self.assertEqual(r2["emitted"], 3)
        self.assertEqual(self._ids(),
                         {"jira:A-1:100", "jira:A-2:101", "jira:A-3:102"})

    def test_poison_issue_skipped_counted_no_wedge(self):
        good = _issue("G-1", "2026-07-10T12:00:00.000+0000", 1,
                      [{"field": "status"}])
        poison = {"key": "BAD", "fields": "corrupt",  # str .get → AttributeError
                  "changelog": {"histories": []}}
        good2 = _issue("G-2", "2026-07-10T12:01:00.000+0000", 2,
                       [{"field": "priority"}])
        c = _make(SearchHttp([good, poison, good2]))
        r = c.poll_once(now=1)
        self.assertEqual(r["emitted"], 2)
        self.assertEqual(r["malformed"], 1)
        self.assertEqual(self._ids(), {"jira:G-1:1", "jira:G-2:2"})
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertTrue(any("malformed" in e for e in hb["errors"]))


# ─── acceptance: double-delivery → one event; kill-mid-batch → no loss ───────

class AcceptanceTests(HomeTestCase):
    def test_double_delivery_yields_one_event(self):
        iss = [_issue("D-1", "2026-07-10T12:00:00.000+0000", 1,
                      [{"field": "assignee", "to": "a", "toString": ACCT}])]
        # `>=` boundary means the same issue re-appears each poll.
        _make(SearchHttp(iss)).poll_once(now=1)
        _make(SearchHttp(iss)).poll_once(now=2)
        self.assertEqual(len(self._drops()), 2)   # at-least-once producer
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 1)
        self.assertEqual(summ["inbox_duplicates"], 1)

    def test_kill_before_cursor_save_loses_nothing(self):
        iss = [_issue("K-1", "2026-07-10T12:00:00.000+0000", 1,
                      [{"field": "status"}]),
               _issue("K-2", "2026-07-10T12:01:00.000+0000", 2,
                      [{"field": "priority"}])]
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
        ids = {json.loads(l)["external_id"]
               for l in eventspine.events_path().read_text().splitlines() if l}
        self.assertEqual(len(ids), 2)


# ─── golden raw→event replay (≥15) ───────────────────────────────────────────

class GoldenReplayTests(unittest.TestCase):
    def test_replay(self):
        files = sorted(JIRA_FIX.glob("*.json"))
        self.assertGreaterEqual(len(files), 15,
                                msg=f"{JIRA_FIX} needs >=15 recorded events")
        for f in files:
            data = json.loads(f.read_text())
            got = jira.issue_to_event(data["raw"], data.get("account", ""))
            got = {k: v for k, v in got.items()
                   if k not in ("id", "raw_path", "epoch")}
            self.assertEqual(got, data["expected"],
                             msg=f"normalization drift in {f.name}")

    def test_expected_are_wellformed_worldevents(self):
        for f in JIRA_FIX.glob("*.json"):
            ev = json.loads(f.read_text())["expected"]
            self.assertEqual(ev["schema"], "world-event/1")
            self.assertTrue(ev["external_id"].startswith("jira:"))
            self.assertEqual(ev["source"], "jira")


if __name__ == "__main__":
    unittest.main()
