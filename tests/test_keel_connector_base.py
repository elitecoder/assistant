"""Tests for src/assistant/connector.py — the Keel M5 connector base contract.

Covers the mandatory contract (design section 9): atomic inbox drop with the
``evt-<source>-<stamp>.json`` name, WorldEvent schema parity with the spine
consumer (byte-for-byte, so the dedup id matches), durable cursor, raw archive
with 7-day retention, heartbeat (last_poll/token_expiry/errors), dry-run/record
flags, config-driven cadence, and the base-owned OAuth refresh-token flow
(refresh-on-expiry + expiry surfaced), all against an injected transport with
NO live network.

New module (sorts after test_daemon), unittest style, tmp $HOME per test.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import connector, eventspine  # noqa: E402


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def sample_event(self, ext="gh-notif:o/r:1:2026-07-10T00:00:00Z"):
        return connector.build_world_event(
            source="github", kind="mention", external_id=ext,
            ts_epoch=1783000000, actor="a", title="t", snippet="s",
            url="u", refs={})


# ─── schema parity with the spine consumer ───────────────────────────────────

class SchemaParityTests(HomeTestCase):
    def test_build_world_event_is_byte_identical_to_spine_base(self):
        ev = self.sample_event()
        base = eventspine._base_event(
            ts_epoch=1783000000, source="github", kind="mention",
            external_id="gh-notif:o/r:1:2026-07-10T00:00:00Z", actor="a",
            title="t", snippet="s", url="u", refs={})
        self.assertEqual(ev, base)

    def test_id_matches_spine_dedup_formula(self):
        ev = self.sample_event()
        self.assertEqual(ev["id"], eventspine.event_id(
            "github", "gh-notif:o/r:1:2026-07-10T00:00:00Z"))

    def test_field_order_and_keys(self):
        self.assertEqual(
            list(self.sample_event().keys()),
            ["schema", "id", "ts", "epoch", "source", "kind", "external_id",
             "actor", "title", "snippet", "url", "refs", "raw_path"])

    def test_snippet_capped_at_2kb(self):
        ev = connector.build_world_event(
            source="gmail", kind="message", external_id="gmail:x",
            ts_epoch=1783000000, snippet="z" * 5000)
        self.assertEqual(len(ev["snippet"]), eventspine.SNIPPET_MAX_CHARS)

    def test_n3_snippet_capped_by_utf8_bytes_not_codepoints(self):
        # A 2000-emoji snippet is 8000 UTF-8 bytes but only 2000 code points —
        # a code-point cap would let it blow ~4× past the 2KB store budget (N3).
        ev = connector.build_world_event(
            source="gmail", kind="message", external_id="gmail:z",
            ts_epoch=1783000000, snippet="\U0001F600" * 2000)
        n = len(ev["snippet"].encode("utf-8"))
        self.assertLessEqual(n, eventspine.SNIPPET_MAX_BYTES)
        self.assertLessEqual(n, 2048)
        # …and the truncation leaves valid UTF-8 (no split trailing sequence).
        ev["snippet"].encode("utf-8").decode("utf-8")


# ─── atomic drop + spine round-trip ──────────────────────────────────────────

class EmitTests(HomeTestCase):
    def test_emit_writes_evt_named_file_atomically(self):
        c = connector.Connector("github", "github")
        p = c.emit(self.sample_event(), raw={"id": "1"})
        self.assertTrue(p.exists())
        self.assertTrue(p.name.startswith("evt-github-"))
        self.assertTrue(p.name.endswith(".json"))
        # no leftover tmp
        self.assertEqual(list(p.parent.glob(".*tmp")), [])

    def test_emitted_event_is_consumed_by_the_spine(self):
        c = connector.Connector("github", "github")
        c.emit(self.sample_event(), raw={"id": "1"})
        summ = eventspine.drain_typed_inbox()
        self.assertEqual(summ["events_appended"], 1)
        rows = [json.loads(l) for l in
                eventspine.events_path().read_text().splitlines()]
        self.assertEqual(rows[0]["source"], "github")

    def test_raw_path_points_into_archive(self):
        c = connector.Connector("github", "github")
        c.emit(self.sample_event(), raw={"id": "1", "reason": "mention"})
        drop = next(eventspine.inbox_dir().glob("evt-*.json"))
        ev = json.loads(drop.read_text())
        self.assertIsNotNone(ev["raw_path"])
        self.assertTrue(Path(ev["raw_path"]).exists())
        self.assertIn("raw", ev["raw_path"])

    def test_dry_run_emits_nothing(self):
        c = connector.Connector("github", "github", dry_run=True)
        p = c.emit(self.sample_event(), raw={"id": "1"})
        self.assertIsNone(p)
        self.assertFalse(eventspine.inbox_dir().exists()
                         and list(eventspine.inbox_dir().glob("evt-*.json")))

    def test_d1_dry_run_is_side_effect_free(self):
        # --dry-run must NOT advance/save the cursor and must NOT write the raw
        # archive — the debug tool must not destroy the events it inspects (D1).
        c = connector.Connector("github", "github", dry_run=True)
        c.save_cursor({"history_id": "9"})
        self.assertFalse(c.cursor_path().exists(), "dry-run wrote cursor.json")
        c.emit(self.sample_event(), raw={"id": "1", "reason": "mention"})
        self.assertFalse(c.raw_dir().exists(), "dry-run wrote raw/ archive")

    def test_n2_raw_path_preserved_through_spine_remint(self):
        # The canonical events.jsonl row's raw_path must resolve to the
        # connector's raw/ UPSTREAM payload, not a normalized copy (N2).
        c = connector.Connector("github", "github")
        c.emit(self.sample_event(), raw={"id": "1", "reason": "mention"})
        eventspine.drain_typed_inbox()
        rows = [json.loads(l) for l in
                eventspine.events_path().read_text().splitlines()]
        rp = rows[0]["raw_path"]
        self.assertIn("connectors/github/raw", rp)
        self.assertTrue(Path(rp).exists())
        self.assertEqual(json.loads(Path(rp).read_text())["id"], "1")


# ─── durable cursor ──────────────────────────────────────────────────────────

class CursorTests(HomeTestCase):
    def test_cursor_roundtrip_and_default_empty(self):
        c = connector.Connector("gmail", "gmail")
        self.assertEqual(c.load_cursor(), {})
        c.save_cursor({"history_id": "42"})
        self.assertEqual(c.load_cursor(), {"history_id": "42"})

    def test_corrupt_cursor_reads_as_empty(self):
        c = connector.Connector("gmail", "gmail")
        c.cursor_path().parent.mkdir(parents=True, exist_ok=True)
        c.cursor_path().write_text("{torn")
        self.assertEqual(c.load_cursor(), {})

    def test_cursor_write_is_atomic_no_tmp_left(self):
        c = connector.Connector("gmail", "gmail")
        c.save_cursor({"a": 1})
        self.assertEqual(list(c.dir().glob("*.tmp")), [])


# ─── raw archive + retention ─────────────────────────────────────────────────

class RawArchiveTests(HomeTestCase):
    def test_archive_writes_dated_dir(self):
        c = connector.Connector("github", "github")
        p = c.archive_raw("gh-notif:o/r:1:t", {"k": "v"}, now=1783000000)
        self.assertTrue(p.exists())
        self.assertRegex(p.parent.name, r"^\d{4}-\d{2}-\d{2}$")

    def test_retention_prunes_old_day_dirs(self):
        c = connector.Connector("github", "github")
        now = time.time()
        old = c.raw_dir() / eventspine.utc_iso(now - 30 * 86400)[:10]
        old.mkdir(parents=True)
        (old / "x.json").write_text("{}")
        fresh = c.raw_dir() / eventspine.utc_iso(now)[:10]
        fresh.mkdir(parents=True)
        (fresh / "y.json").write_text("{}")
        removed = c.prune_raw(now=now)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


# ─── record → fixtures ───────────────────────────────────────────────────────

class RecordTests(HomeTestCase):
    def test_record_writes_a_replay_fixture(self):
        c = connector.Connector("github", "github", record=True)
        # point the repo fixtures dir at a tmp location by monkeypatching
        target = self.home / "fx"
        orig = connector.fixtures_dir
        connector.fixtures_dir = lambda name: target  # type: ignore
        try:
            c.emit(self.sample_event(), raw={"id": "1", "reason": "mention"})
        finally:
            connector.fixtures_dir = orig  # type: ignore
        files = list(target.glob("rec-*.json"))
        self.assertEqual(len(files), 1)
        fx = json.loads(files[0].read_text())
        self.assertIn("raw", fx)
        self.assertIn("expected", fx)
        self.assertNotIn("id", fx["expected"])  # volatile fields stripped


# ─── heartbeat ───────────────────────────────────────────────────────────────

class HeartbeatTests(HomeTestCase):
    def test_heartbeat_records_poll_and_staleness_budget(self):
        c = connector.Connector("github", "github",
                                config={"cadence_sec": 60, "stale_factor": 6})
        c.write_heartbeat(last_poll_epoch=1783000000, event_count=3,
                          poll_count=1)
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["source"], "github")
        self.assertEqual(hb["last_poll_epoch"], 1783000000)
        self.assertEqual(hb["event_count"], 3)
        self.assertTrue(hb["ok"])
        self.assertEqual(hb["stale_after_sec"], max(900, 60 * 6))

    def test_heartbeat_errors_flip_ok_false(self):
        c = connector.Connector("github", "github")
        c.write_heartbeat(last_poll_epoch=1783000000, errors=["boom"])
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertFalse(hb["ok"])
        self.assertEqual(hb["errors"], ["boom"])

    def test_heartbeat_surfaces_token_expiry(self):
        c = connector.Connector("gmail", "gmail")
        c.write_heartbeat(last_poll_epoch=1783000000,
                          token_expiry_epoch=1783003600)
        hb = json.loads(c.heartbeat_path().read_text())
        self.assertEqual(hb["token_expiry_epoch"], 1783003600)
        self.assertEqual(hb["token_expiry"], eventspine.utc_iso(1783003600))


# ─── config-driven cadence (never hardcoded) ─────────────────────────────────

class ConfigTests(HomeTestCase):
    def _write_config(self, obj):
        p = connector.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))

    def test_defaults_when_no_config(self):
        cfg = connector.load_connector_config("github")
        self.assertEqual(cfg["cadence_sec"], connector.DEFAULT_CADENCE_SEC)

    def test_per_connector_overrides_defaults_block(self):
        self._write_config({"connectors": {
            "_defaults": {"cadence_sec": 120},
            "github": {"cadence_sec": 45}}})
        self.assertEqual(
            connector.load_connector_config("github")["cadence_sec"], 45)
        self.assertEqual(
            connector.load_connector_config("gmail")["cadence_sec"], 120)

    def test_broken_config_falls_back_to_defaults(self):
        p = connector.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        self.assertEqual(
            connector.load_connector_config("github")["cadence_sec"],
            connector.DEFAULT_CADENCE_SEC)


# ─── OAuth refresh-token flow (injected transport, no network) ───────────────

class OAuthTests(HomeTestCase):
    def _seed_token(self, *, access="OLD", expiry_epoch=1000,
                    refresh="R"):
        p = self.home / "token.json"
        p.write_text(json.dumps({
            "access_token": access, "refresh_token": refresh,
            "expiry_epoch": expiry_epoch, "client_id": "cid",
            "client_secret": "sec"}))
        return p

    def test_refresh_on_expiry_calls_endpoint_once(self):
        p = self._seed_token(expiry_epoch=1000)
        calls = []

        def fake(uri, form):
            calls.append((uri, form))
            return {"access_token": "NEW", "expires_in": 3600}

        mgr = connector.OAuthTokenManager(p, transport=fake)
        tok = mgr.access_token(now=2000)  # 2000 >> expiry 1000 → refresh
        self.assertEqual(tok, "NEW")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["grant_type"], "refresh_token")
        self.assertEqual(calls[0][1]["refresh_token"], "R")
        self.assertEqual(mgr.expiry_epoch(), 2000 + 3600)

    def test_valid_token_is_not_refreshed(self):
        p = self._seed_token(access="GOOD", expiry_epoch=1_000_000)
        calls = []
        mgr = connector.OAuthTokenManager(
            p, transport=lambda u, f: calls.append(1) or {})
        self.assertEqual(mgr.access_token(now=1000), "GOOD")
        self.assertEqual(calls, [])

    def test_skew_forces_early_refresh(self):
        p = self._seed_token(access="OLD", expiry_epoch=2000)
        mgr = connector.OAuthTokenManager(
            p, skew_sec=300,
            transport=lambda u, f: {"access_token": "NEW", "expires_in": 100})
        # now=1750 is < expiry(2000) but within the 300s skew window → refresh
        self.assertEqual(mgr.access_token(now=1750), "NEW")

    def test_rotated_refresh_token_is_kept(self):
        p = self._seed_token(expiry_epoch=1000)
        mgr = connector.OAuthTokenManager(
            p, transport=lambda u, f: {
                "access_token": "NEW", "expires_in": 3600,
                "refresh_token": "R2"})
        mgr.access_token(now=2000)
        self.assertEqual(json.loads(p.read_text())["refresh_token"], "R2")

    def test_missing_token_cache_raises_oauth_error(self):
        mgr = connector.OAuthTokenManager(self.home / "nope.json",
                                          transport=lambda u, f: {})
        with self.assertRaises(connector.OAuthError):
            mgr.access_token(now=1)

    def test_token_file_written_owner_only(self):
        p = self._seed_token(expiry_epoch=1000)
        mgr = connector.OAuthTokenManager(
            p, transport=lambda u, f: {"access_token": "N", "expires_in": 1})
        mgr.refresh(now=2000)
        self.assertEqual(os.stat(p).st_mode & 0o077, 0)

    def test_sec3a_tmp_never_group_or_other_readable_during_write(self):
        # Sample the tmp's mode at the instant before os.replace: it must be
        # 0600 the WHOLE time (created 0600 atomically), never a 0644 window
        # before a chmod (SEC3a).
        p = self._seed_token(expiry_epoch=1000)
        modes = []
        orig = connector.os.replace

        def spy(src, dst):
            modes.append(os.stat(src).st_mode & 0o777)
            return orig(src, dst)

        connector.os.replace = spy
        try:
            connector.OAuthTokenManager(
                p, transport=lambda u, f: {"access_token": "N",
                                           "expires_in": 1}).refresh(now=2000)
        finally:
            connector.os.replace = orig
        self.assertTrue(modes)
        for m in modes:
            self.assertEqual(m & 0o077, 0, f"tmp mode {oct(m)} exposed")

    def test_sec3b_concurrent_saves_use_unique_tmp_names(self):
        p = self._seed_token(expiry_epoch=1000)
        names = []
        orig = connector.os.replace

        def spy(src, dst):
            names.append(str(src))
            return orig(src, dst)

        connector.os.replace = spy
        try:
            mgr = connector.OAuthTokenManager(
                p, transport=lambda u, f: {"access_token": "N",
                                           "expires_in": 1})
            mgr.refresh(now=2000)
            mgr.refresh(now=3000)
        finally:
            connector.os.replace = orig
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])  # per-writer unique tmp

    def test_o1_urlerror_refresh_becomes_oauth_error(self):
        # Network-down (URLError/timeout) must be wrapped as OAuthError so the
        # token-expiry signal survives and --once doesn't crash (O1).
        orig = connector.urllib.request.urlopen

        def boom(*a, **k):
            raise urllib.error.URLError("network down")

        connector.urllib.request.urlopen = boom
        try:
            with self.assertRaises(connector.OAuthError):
                connector._urllib_token_post(
                    "https://x/token", {"grant_type": "refresh_token"})
        finally:
            connector.urllib.request.urlopen = orig

    def test_o1_bad_json_refresh_becomes_oauth_error(self):
        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=None):
                return b"<html>not json</html>"

        orig = connector.urllib.request.urlopen
        connector.urllib.request.urlopen = lambda *a, **k: _R()
        try:
            with self.assertRaises(connector.OAuthError):
                connector._urllib_token_post("https://x/token", {})
        finally:
            connector.urllib.request.urlopen = orig


# ─── OP2 bounded read / OP1 backoff / retry-after (LOW) ───────────────────────

class BoundedReadTests(HomeTestCase):
    def test_op2_response_read_is_capped(self):
        captured = {}

        class _R:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=None):
                captured["n"] = n
                return b"{}"

        orig = connector.urllib.request.urlopen
        connector.urllib.request.urlopen = lambda *a, **k: _R()
        try:
            connector.urllib_transport("GET", "https://x")
        finally:
            connector.urllib.request.urlopen = orig
        self.assertEqual(captured["n"], connector.MAX_RESPONSE_BYTES)


class RetryAfterParseTests(HomeTestCase):
    def test_delta_seconds(self):
        self.assertEqual(
            connector.parse_retry_after({"Retry-After": "45"}, now=1000), 45.0)

    def test_x_ratelimit_reset_epoch(self):
        self.assertEqual(
            connector.parse_retry_after({"X-RateLimit-Reset": "1120"},
                                        now=1000), 120.0)

    def test_none_when_absent(self):
        self.assertIsNone(connector.parse_retry_after({}, now=1000))


class BackoffLoopTests(HomeTestCase):
    def _stub(self, results):
        class _Stub(connector.Connector):
            def __init__(s):
                super().__init__("x", "x", config={"cadence_sec": 60})
                s._q = list(results)
                s._i = 0

            def poll_once(s, now=None):
                r = s._q[s._i]
                s._i += 1
                return r

        return _Stub()

    def test_op1_honors_retry_after(self):
        c = self._stub([{"status": "status_429", "retry_after_sec": 120},
                        {"status": "ok"}])
        sleeps = []
        c.run_forever(max_iterations=2, sleep=lambda s: sleeps.append(s))
        self.assertEqual(sleeps, [120.0])  # server backoff beats cadence

    def test_op1_bounded_exponential_backoff_grows(self):
        c = self._stub([{"status": "http_error"}, {"status": "http_error"},
                        {"status": "ok"}])
        sleeps = []
        c.run_forever(max_iterations=3, sleep=lambda s: sleeps.append(s))
        self.assertEqual(sleeps, [60.0, 120.0])  # grew under sustained error

    def test_op1_healthy_polls_hold_cadence(self):
        c = self._stub([{"status": "ok"}, {"status": "ok"}])
        sleeps = []
        c.run_forever(max_iterations=2, sleep=lambda s: sleeps.append(s))
        self.assertEqual(sleeps, [60.0])


class NotConfiguredReCheckTests(HomeTestCase):
    """F3: an OPTIONAL connector nobody set up (poll returns not_configured)
    must re-check on a LONG cadence — never the old exit(0) that hot-respawned
    every ~10s under the KeepAlive plists, and never the error backoff — and
    must auto-recover to ok the moment the owner configures it."""

    def _stub(self, results):
        class _Stub(connector.Connector):
            def __init__(s):
                super().__init__("x", "x",
                                 config={"cadence_sec": 60,
                                         "not_configured_recheck_sec": 300})
                s._q = list(results)
                s._i = 0

            def poll_once(s, now=None):
                r = s._q[s._i]
                s._i += 1
                return r

        return _Stub()

    def test_f3_unconfigured_uses_long_recheck_not_hot_spin(self):
        # not_configured sleeps the long recheck (300), never the 60s cadence and
        # never the ~10s KeepAlive respawn the old exit(0) caused.
        c = self._stub([{"status": "not_configured"},
                        {"status": "not_configured"}])
        sleeps = []
        c.run_forever(max_iterations=2, sleep=lambda s: sleeps.append(s))
        self.assertEqual(sleeps, [300.0])

    def test_f3_auto_recovers_to_ok_on_next_recheck(self):
        # Once the owner runs --authorize / gh auth login, the next poll returns
        # ok and the daemon transitions to the normal cadence — NO relaunch.
        c = self._stub([{"status": "not_configured"}, {"status": "ok"},
                        {"status": "ok"}])
        sleeps = []
        c.run_forever(max_iterations=3, sleep=lambda s: sleeps.append(s))
        self.assertEqual(sleeps, [300.0, 60.0])


if __name__ == "__main__":
    unittest.main()
