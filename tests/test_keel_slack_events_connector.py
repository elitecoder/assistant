"""Tests for bin/connectors/slack-events.py — the M5 wave-2 Slack events
connector.

Proves, against a spool directory + an injected wiring check (no network, no
Slack): mechanical kind derivation (app_mention/dm/message from Slack's own
event metadata), the not_configured path when the Bolt app isn't wired (mirrors
github's token provider), spool consumption + watermark discipline (a transient
drop failure stops the batch and leaves the rest for the next poll), poison
payload skip, dry-run side-effect-freeness (the spool is NEVER consumed),
double-delivery→one-event, and the ≥15 golden raw→event replay fixtures. Also
re-asserts the read-only guarantee for THIS connector (no chat.post / send).

New module, unittest style, tmp $HOME per test.
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

from assistant import eventspine  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


slack = _load("slack_test", "bin/connectors/slack-events.py")
SLACK_FIX = REPO / "evals" / "connectors" / "slack" / "fixtures"


def _ev(etype, channel, ts, channel_type=None, text="hi", **kw):
    ev = {"type": etype, "channel": channel, "ts": ts, "user": "U",
          "text": text}
    if channel_type:
        ev["channel_type"] = channel_type
    ev.update(kw)
    return {"event": ev}


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

    def _make(self, wired=True):
        return slack.SlackEventsConnector(wired_check=lambda: wired)

    def _spool(self, c, name, payload):
        d = c.spool_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        if isinstance(payload, str):
            p.write_text(payload)
        else:
            p.write_text(json.dumps(payload))
        return p

    def _drops(self):
        return sorted((self.home / ".assistant/inbox").glob("evt-slack-*.json"))

    def _kinds(self):
        return sorted(json.loads(d.read_text())["kind"] for d in self._drops())

    def _ids(self):
        return {json.loads(d.read_text())["external_id"] for d in self._drops()}


# ─── normalization ───────────────────────────────────────────────────────────

class NormalizeTests(unittest.TestCase):
    def test_app_mention_kind(self):
        ev = slack.slack_event_to_event(_ev("app_mention", "C1", "1.1"))
        self.assertEqual(ev["kind"], "app_mention")
        self.assertEqual(ev["external_id"], "slack:C1:1.1")

    def test_dm_kind(self):
        ev = slack.slack_event_to_event(
            _ev("message", "D1", "2.2", channel_type="im"))
        self.assertEqual(ev["kind"], "dm")

    def test_channel_noise_kind(self):
        ev = slack.slack_event_to_event(
            _ev("message", "C2", "3.3", channel_type="channel"))
        self.assertEqual(ev["kind"], "message")

    def test_refs_and_thread(self):
        ev = slack.slack_event_to_event(
            _ev("app_mention", "C1", "4.4", thread_ts="0.0"))
        self.assertEqual(ev["refs"]["channel"], "C1")
        self.assertEqual(ev["refs"]["slack_ts"], "4.4")
        self.assertEqual(ev["refs"]["thread_ts"], "0.0")

    def test_permalink_from_payload_wins(self):
        p = _ev("app_mention", "C1", "5.5")
        p["permalink"] = "https://x.slack.com/p"
        self.assertEqual(slack.slack_event_to_event(p)["url"],
                         "https://x.slack.com/p")

    def test_archive_url_fallback(self):
        ev = slack.slack_event_to_event(_ev("app_mention", "C1", "6.6"))
        self.assertEqual(ev["url"], "https://slack.com/archives/C1/p66")


# ─── not_configured (Bolt app not wired) ─────────────────────────────────────

class NotConfiguredTests(HomeTestCase):
    def test_unwired_is_quiet_not_configured(self):
        c = self._make(wired=False)
        res = c.poll_once(now=1)
        self.assertEqual(res["status"], "not_configured")
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["status"], "not_configured")
        self.assertTrue(hb["ok"])
        self.assertEqual(hb["errors"], [])

    def test_default_wired_check_reads_env(self):
        os.environ.pop("SLACK_BOT_TOKEN", None)
        self.assertFalse(slack.slack_is_wired())
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-x"
        try:
            self.assertTrue(slack.slack_is_wired())
        finally:
            os.environ.pop("SLACK_BOT_TOKEN", None)


# ─── spool consumption + watermark discipline ────────────────────────────────

class SpoolTests(HomeTestCase):
    def test_normalizes_all_kinds_and_drains_spool(self):
        c = self._make()
        self._spool(c, "01.json", _ev("app_mention", "C1", "1.1"))
        self._spool(c, "02.json", _ev("message", "D1", "2.2",
                                       channel_type="im"))
        self._spool(c, "03.json", _ev("message", "C2", "3.3",
                                       channel_type="channel"))
        r = c.poll_once(now=1)
        self.assertEqual(r["emitted"], 3)
        self.assertEqual(self._kinds(), ["app_mention", "dm", "message"])
        self.assertEqual(list(c.spool_dir().iterdir()), [])  # drained

    def test_poison_payload_skipped_counted_removed(self):
        c = self._make()
        self._spool(c, "01.json", _ev("app_mention", "C1", "1.1"))
        self._spool(c, "02.json", "{not json")           # corrupt
        self._spool(c, "03.json", _ev("app_mention", "C3", "3.3"))
        r = c.poll_once(now=1)
        self.assertEqual(r["emitted"], 2)
        self.assertEqual(r["malformed"], 1)
        self.assertEqual(list(c.spool_dir().iterdir()), [])  # poison removed
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])

    def test_transient_drop_failure_leaves_rest_for_next_poll(self):
        c = self._make()
        self._spool(c, "01.json", _ev("app_mention", "C1", "1.1"))
        self._spool(c, "02.json", _ev("app_mention", "C2", "2.2"))
        self._spool(c, "03.json", _ev("app_mention", "C3", "3.3"))
        orig = c.emit
        n = {"i": 0}

        def femit(ev, raw=None, now=None):
            n["i"] += 1
            if n["i"] == 2:
                raise OSError("drop failed")
            return orig(ev, raw=raw, now=now)
        c.emit = femit  # type: ignore
        r = c.poll_once(now=1)
        self.assertEqual(r["emitted"], 1)                # only the first
        # events 2 and 3 remain spooled (nothing lost)
        remaining = sorted(p.name for p in c.spool_dir().iterdir())
        self.assertEqual(remaining, ["02.json", "03.json"])

    def test_dry_run_never_consumes_spool(self):
        c = slack.SlackEventsConnector(wired_check=lambda: True, dry_run=True)
        self._spool(c, "01.json", _ev("app_mention", "C1", "1.1"))
        c.poll_once(now=1)
        self.assertEqual([p.name for p in c.spool_dir().iterdir()],
                         ["01.json"])                    # untouched
        self.assertEqual(self._drops(), [])              # nothing dropped
        self.assertFalse(c.cursor_path().exists())       # cursor not saved


# ─── acceptance: double-delivery → one event ─────────────────────────────────

class AcceptanceTests(HomeTestCase):
    def test_double_delivery_yields_one_event(self):
        # The Bolt app can spool the same event twice (at-least-once), e.g. a
        # re-delivery across a reconnect; the spine collapses them by the stable
        # external_id. Two separate polls so the two drops get distinct inbox
        # filenames (an in-poll exact dup would atomically replace itself).
        c = self._make()
        self._spool(c, "01.json", _ev("app_mention", "C1", "9.9"))
        c.poll_once(now=1)
        self._spool(c, "02.json", _ev("app_mention", "C1", "9.9"))  # dup ts
        c.poll_once(now=2)
        self.assertEqual(len(self._drops()), 2)
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 1)
        self.assertEqual(summ["inbox_duplicates"], 1)


# ─── read-only guarantee for THIS connector ──────────────────────────────────

class ReadOnlyTests(unittest.TestCase):
    def test_no_send_or_post_call_sites(self):
        text = (REPO / "bin" / "connectors" / "slack-events.py").read_text()
        for forbidden in ("chat.postMessage", ".send_message(",
                          "messages.send"):
            self.assertNotIn(forbidden, text)
        for verb in ('"POST"', '"PUT"', '"DELETE"'):
            self.assertNotIn(verb, text)


# ─── golden raw→event replay (≥15) ───────────────────────────────────────────

class GoldenReplayTests(unittest.TestCase):
    def test_replay(self):
        files = sorted(SLACK_FIX.glob("*.json"))
        self.assertGreaterEqual(len(files), 15,
                                msg=f"{SLACK_FIX} needs >=15 recorded events")
        for f in files:
            data = json.loads(f.read_text())
            got = slack.slack_event_to_event(data["raw"])
            got = {k: v for k, v in got.items()
                   if k not in ("id", "raw_path", "epoch")}
            self.assertEqual(got, data["expected"],
                             msg=f"normalization drift in {f.name}")

    def test_expected_are_wellformed_worldevents(self):
        for f in SLACK_FIX.glob("*.json"):
            ev = json.loads(f.read_text())["expected"]
            self.assertEqual(ev["schema"], "world-event/1")
            self.assertTrue(re.match(r"^slack:", ev["external_id"]))
            self.assertEqual(ev["source"], "slack")


if __name__ == "__main__":
    unittest.main()
