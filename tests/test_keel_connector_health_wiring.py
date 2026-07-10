"""Heartbeat → world.json → brief health wiring (Keel M5, design item 5).

A dead connector (stale last_poll) or an expiring OAuth token must be visible in
the brief's health section — and joined into world.json for the dashboard —
within one morning. This proves the derivation on both consumers.

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

from assistant import brief, connector  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def write_hb(self, name, hb):
        d = self.home / ".assistant" / "connectors" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "heartbeat.json").write_text(json.dumps(hb))


NOW = 1783080000


class BriefDerivationTests(HomeTestCase):
    def test_fresh_connector_is_ok(self):
        self.write_hb("github", {
            "source": "github", "last_poll": "x", "last_poll_epoch": NOW - 30,
            "stale_after_sec": 900, "ok": True})
        hb = brief._connector_heartbeats(NOW)["github"]
        self.assertFalse(hb["stale"])
        self.assertTrue(hb["ok"])

    def test_stale_connector_flagged(self):
        self.write_hb("github", {
            "source": "github", "last_poll_epoch": NOW - 5000,
            "stale_after_sec": 900, "ok": True})
        hb = brief._connector_heartbeats(NOW)["github"]
        self.assertTrue(hb["stale"])
        self.assertFalse(hb["ok"])

    def test_expired_token_flagged(self):
        self.write_hb("gmail", {
            "source": "gmail", "last_poll_epoch": NOW - 10,
            "stale_after_sec": 900, "ok": True,
            "token_expiry_epoch": NOW - 1, "token_expiry": "past"})
        hb = brief._connector_heartbeats(NOW)["gmail"]
        self.assertTrue(hb["token_expired"])
        self.assertFalse(hb["ok"])

    def test_absent_dir_is_empty(self):
        self.assertEqual(brief._connector_heartbeats(NOW), {})

    def test_build_health_includes_connectors(self):
        self.write_hb("github", {"source": "github",
                                 "last_poll_epoch": NOW - 5000,
                                 "stale_after_sec": 900})
        health = brief._build_health([], NOW)
        self.assertIn("github", health["connectors"])
        self.assertTrue(health["connectors"]["github"]["stale"])


class ClassifyStateTests(unittest.TestCase):
    """The canonical tri-state (not_configured|ok|error) — the ONE place the
    model lives, shared by world-scanner, the brief and the dashboard."""

    def test_no_heartbeat_is_not_configured(self):
        v = connector.classify_connector(None, NOW)
        self.assertEqual(v["status"], connector.STATE_NOT_CONFIGURED)
        self.assertFalse(v["stale"])   # never an alarm
        self.assertFalse(v["ok"])

    def test_status_not_configured_is_quiet_even_when_ancient(self):
        # An opted-out connector wrote ONE beat then exited; its last_poll WILL
        # age past stale_after — that must NOT rot into a stale/error alarm.
        v = connector.classify_connector({
            "source": "gmail", "status": "not_configured",
            "last_poll_epoch": NOW - 999999, "stale_after_sec": 900,
            "ok": True}, NOW)
        self.assertEqual(v["status"], connector.STATE_NOT_CONFIGURED)
        self.assertFalse(v["stale"])
        self.assertFalse(v["token_expired"])

    def test_configured_fresh_is_ok(self):
        v = connector.classify_connector({
            "source": "github", "last_poll_epoch": NOW - 10,
            "stale_after_sec": 900, "ok": True}, NOW)
        self.assertEqual(v["status"], connector.STATE_OK)
        self.assertTrue(v["ok"])

    def test_configured_stale_is_error(self):
        v = connector.classify_connector({
            "source": "github", "last_poll_epoch": NOW - 99999,
            "stale_after_sec": 900, "ok": True}, NOW)
        self.assertEqual(v["status"], connector.STATE_ERROR)
        self.assertTrue(v["stale"])

    def test_configured_expired_token_is_error(self):
        v = connector.classify_connector({
            "source": "gmail", "last_poll_epoch": NOW - 10,
            "stale_after_sec": 900, "ok": True,
            "token_expiry_epoch": NOW - 1}, NOW)
        self.assertEqual(v["status"], connector.STATE_ERROR)
        self.assertTrue(v["token_expired"])


class BriefQuietNotConfiguredTests(HomeTestCase):
    def test_not_configured_is_not_flagged_a_problem(self):
        # gmail opted-out, its last_poll is ancient (would be "stale" if it were
        # configured). The brief must NOT flag it as stale/error.
        self.write_hb("gmail", {
            "source": "gmail", "status": "not_configured",
            "last_poll_epoch": NOW - 999999, "stale_after_sec": 900,
            "ok": True})
        v = brief._connector_heartbeats(NOW)["gmail"]
        self.assertEqual(v["status"], "not_configured")
        self.assertFalse(v["stale"])

    def test_configured_stale_ok_connector_still_flagged(self):
        # The existing stale/expiry alerting for a once-ok connector survives.
        self.write_hb("github", {
            "source": "github", "last_poll_epoch": NOW - 99999,
            "stale_after_sec": 900, "ok": True})
        v = brief._connector_heartbeats(NOW)["github"]
        self.assertEqual(v["status"], "error")
        self.assertTrue(v["stale"])
        self.assertFalse(v["ok"])


class WorldScannerJoinTests(HomeTestCase):
    def _summary(self):
        from datetime import datetime, timezone
        ws = _load("world_scanner_hw", "bin/world-scanner.py")
        return ws.build_connectors_summary(
            datetime.fromtimestamp(NOW, tz=timezone.utc))

    def test_world_scanner_joins_connector_heartbeats(self):
        self.write_hb("gmail", {
            "source": "gmail", "last_poll": "x", "last_poll_epoch": NOW - 20,
            "stale_after_sec": 900, "ok": True,
            "token_expiry_epoch": NOW + 3600, "token_expiry": "future"})
        self.write_hb("github", {
            "source": "github", "last_poll_epoch": NOW - 99999,
            "stale_after_sec": 900})
        summary = self._summary()
        self.assertFalse(summary["gmail"]["stale"])
        self.assertTrue(summary["gmail"]["ok"])
        self.assertTrue(summary["github"]["stale"])
        self.assertFalse(summary["github"]["ok"])

    def test_carries_status_last_poll_token_expiry(self):
        self.write_hb("gmail", {
            "source": "gmail", "last_poll": "2026-01-01T00:00:00Z",
            "last_poll_epoch": NOW - 20, "stale_after_sec": 900, "ok": True,
            "token_expiry": "2026-06-01T00:00:00Z",
            "token_expiry_epoch": NOW + 3600})
        v = self._summary()["gmail"]
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["last_poll"], "2026-01-01T00:00:00Z")
        self.assertEqual(v["token_expiry"], "2026-06-01T00:00:00Z")

    def test_known_connector_without_heartbeat_is_not_configured(self):
        # gmail wrote a heartbeat; github NEVER ran (no heartbeat file at all).
        # world.json must still list github, as not_configured (available), NOT
        # as an error/stale.
        self.write_hb("gmail", {
            "source": "gmail", "last_poll_epoch": NOW - 20,
            "stale_after_sec": 900, "ok": True})
        summary = self._summary()
        self.assertIn("github", summary)
        self.assertEqual(summary["github"]["status"], "not_configured")
        self.assertFalse(summary["github"]["stale"])
        self.assertEqual(summary["gmail"]["status"], "ok")

    def test_fresh_install_all_known_connectors_available(self):
        # Nothing configured at all → every known connector is not_configured.
        summary = self._summary()
        self.assertEqual(
            {n: v["status"] for n, v in summary.items()},
            {"github": "not_configured", "gmail": "not_configured"})


if __name__ == "__main__":
    unittest.main()
