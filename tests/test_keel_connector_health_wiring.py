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

from assistant import brief  # noqa: E402


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


class WorldScannerJoinTests(HomeTestCase):
    def test_world_scanner_joins_connector_heartbeats(self):
        ws = _load("world_scanner_hw", "bin/world-scanner.py")
        self.write_hb("gmail", {
            "source": "gmail", "last_poll": "x", "last_poll_epoch": NOW - 20,
            "stale_after_sec": 900, "ok": True,
            "token_expiry_epoch": NOW + 3600, "token_expiry": "future"})
        self.write_hb("github", {
            "source": "github", "last_poll_epoch": NOW - 99999,
            "stale_after_sec": 900})
        from datetime import datetime, timezone
        summary = ws.build_connectors_summary(
            datetime.fromtimestamp(NOW, tz=timezone.utc))
        self.assertFalse(summary["gmail"]["stale"])
        self.assertTrue(summary["gmail"]["ok"])
        self.assertTrue(summary["github"]["stale"])
        self.assertFalse(summary["github"]["ok"])


if __name__ == "__main__":
    unittest.main()
