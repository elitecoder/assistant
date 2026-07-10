"""Tests for src/assistant/eventspine.py — the typed WorldEvent inbox consumer
(Keel M1) — plus its two integration points: event-priority promotion in
bin/pick-ws-batch.py and the events health section in bin/world-scanner.py.

unittest style so the suite runs under `python3 -m unittest discover tests`
(conftest.py is pytest-only, so src/ goes on sys.path here). Everything runs
against a tmp $HOME — eventspine computes every path per call from $HOME, so
no module reload is needed between tests; the hyphenated bin/ scripts ARE
reloaded per test because they bind paths at import (same pattern as
test_pulse.py / test_pickers_in_process.py).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from assistant import eventspine  # noqa: E402


def load_script(home: Path, script: str):
    """Import a hyphenated bin/ script with HOME pointed at `home` (its path
    constants bind at import)."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(
        f"_{script}", str(REPO / "bin" / script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SpineTestCase(unittest.TestCase):
    """Tmp-HOME sandbox: eventspine resolves paths per call, so setting the
    env var is the whole fixture."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.inbox = self.home / ".assistant/inbox"
        self.inbox.mkdir(parents=True)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    # ── helpers ─────────────────────────────────────────────────────────

    def drop_signal(self, name="cmux-workspace-7-a.json", ws_ref="workspace:7",
                    signal="needs_input", ts="2026-07-09T12:00:00Z",
                    pattern="Notification", snippet="approve?") -> Path:
        """A cmux-watcher-shaped inbox drop (the real producer's schema)."""
        p = self.inbox / name
        p.write_text(json.dumps({
            "ts": ts, "event": "workspace_signal", "ws_ref": ws_ref,
            "signal_type": signal, "pattern_matched": pattern,
            "screen_snippet": snippet,
        }, indent=2))
        return p

    def events(self) -> list:
        path = self.home / ".assistant/events.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]

    def ledger_rows(self, kind) -> list:
        path = self.home / ".assistant/actions-ledger.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        return [r for r in rows if r.get("kind") == kind]


# ─── normalization ──────────────────────────────────────────────────────────

class NormalizeTests(SpineTestCase):
    def test_signal_external_id_buckets_by_time(self):
        # Two pings inside one 10-min bucket → same external_id (one event);
        # a ping in the next bucket → a new one.
        base = {"event": "workspace_signal", "ws_ref": "workspace:3",
                "signal_type": "needs_input"}
        a = eventspine.normalize_inbox_item(
            "a.json", dict(base, ts="2026-07-09T12:00:05Z"), 0)
        b = eventspine.normalize_inbox_item(
            "b.json", dict(base, ts="2026-07-09T12:09:55Z"), 0)
        c = eventspine.normalize_inbox_item(
            "c.json", dict(base, ts="2026-07-09T12:10:05Z"), 0)
        self.assertEqual(a["external_id"], b["external_id"])
        self.assertNotEqual(a["external_id"], c["external_id"])
        self.assertTrue(a["external_id"].startswith(
            "cmux:workspace:3:needs_input:"))

    def test_id_is_sha256_of_source_and_external_id(self):
        ev = eventspine.normalize_inbox_item(
            "a.json", {"event": "workspace_signal", "ws_ref": "workspace:1",
                       "signal_type": "work_complete"}, 1000)
        self.assertEqual(
            ev["id"], eventspine.event_id("cmux", ev["external_id"]))
        self.assertEqual(len(ev["id"]), 64)

    def test_snippet_capped_at_2kb(self):
        ev = eventspine.normalize_inbox_item(
            "a.json", {"event": "workspace_signal", "ws_ref": "workspace:1",
                       "signal_type": "needs_input",
                       "screen_snippet": "x" * 10_000}, 1000)
        self.assertEqual(len(ev["snippet"]), eventspine.SNIPPET_MAX_CHARS)

    def test_world_event_passthrough_recomputes_id(self):
        ev = eventspine.normalize_inbox_item(
            "evt-github-1.json",
            {"schema": "world-event/1", "source": "github",
             "kind": "notification", "external_id": "gh-notif:o/r:1:t",
             "id": "attacker-chosen", "title": "PR review",
             "refs": {"pr": 42}}, 1000)
        self.assertEqual(
            ev["id"], eventspine.event_id("github", "gh-notif:o/r:1:t"))
        self.assertEqual(ev["refs"], {"pr": 42})

    def test_unrecognized_shape_raises(self):
        with self.assertRaises(ValueError):
            eventspine.normalize_inbox_item("mystery.json", {"foo": 1}, 1000)
        with self.assertRaises(ValueError):
            eventspine.normalize_inbox_item("mystery.json", [1, 2], 1000)

    def test_crash_event_identity_comes_from_filename_epoch(self):
        # workspace-watcher rewrites the body as the resume progresses; the
        # filename epoch is the stable identity.
        ev = eventspine.normalize_crash_event(
            Path("/x/workspace-5-1712345678.json"),
            {"schema_version": 2, "workspace_ref": "workspace:5",
             "cause": "crash", "name": "My WS", "resume": {"ok": True}})
        self.assertEqual(ev["external_id"], "cmux:workspace:5:closed:1712345678")
        self.assertEqual(ev["kind"], "workspace_closed")
        self.assertEqual(ev["refs"], {"ws_ref": "workspace:5"})


# ─── the drain: well-formed / malformed / duplicate / legacy ────────────────

class DrainTests(SpineTestCase):
    def test_well_formed_event_consumed_exactly_once(self):
        p = self.drop_signal()
        s1 = eventspine.drain_typed_inbox()
        self.assertEqual(s1["events_appended"], 1)
        self.assertFalse(p.exists(), "consumed drop must be unlinked")
        rows = self.events()
        self.assertEqual(len(rows), 1)
        ev = rows[0]
        self.assertEqual(ev["schema"], "world-event/1")
        self.assertEqual(ev["source"], "cmux")
        self.assertEqual(ev["kind"], "needs_input")
        self.assertEqual(ev["refs"]["ws_ref"], "workspace:7")
        # Raw drop archived before the unlink, byte-identical.
        raw = Path(ev["raw_path"])
        self.assertTrue(raw.exists())
        self.assertEqual(json.loads(raw.read_text())["ws_ref"], "workspace:7")
        # A second drain finds nothing new.
        s2 = eventspine.drain_typed_inbox()
        self.assertEqual(s2["events_appended"], 0)
        self.assertEqual(len(self.events()), 1)

    def test_malformed_file_quarantined_and_drain_continues(self):
        bad = self.inbox / "cmux-broken.json"
        bad.write_text("{ not json at all")
        odd = self.inbox / "evt-mystery.json"
        odd.write_text(json.dumps({"foo": "parses but means nothing"}))
        good = self.drop_signal()
        s = eventspine.drain_typed_inbox()
        # The good drop was still consumed — malformed neighbors never abort.
        self.assertEqual(s["events_appended"], 1)
        self.assertEqual(s["inbox_quarantined"], 2)
        self.assertFalse(bad.exists())
        self.assertFalse(odd.exists())
        self.assertFalse(good.exists())
        # Quarantined, not vanished — file content preserved + ledgered.
        qfiles = list(eventspine.quarantine_dir().glob("*.json"))
        self.assertEqual(len(qfiles), 2)
        self.assertEqual(len(self.ledger_rows("eventspine-quarantine")), 2)

    def test_duplicate_external_id_yields_single_row(self):
        # Same ws/signal/ts-bucket dropped twice (watcher re-ping) → one row,
        # both files disposed of.
        a = self.drop_signal(name="cmux-a.json", ts="2026-07-09T12:00:01Z")
        b = self.drop_signal(name="cmux-b.json", ts="2026-07-09T12:01:30Z")
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 1)
        self.assertEqual(s["inbox_duplicates"], 1)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(len(self.events()), 1)

    def test_duplicate_detected_across_drains_without_index(self):
        # Crash window: row appended but the dedup-index write never landed.
        # The tail scan of events.jsonl must still catch the replay.
        self.drop_signal(name="cmux-a.json")
        eventspine.drain_typed_inbox()
        eventspine.dedup_index_path().unlink()
        self.drop_signal(name="cmux-a2.json")  # same bucket → same id
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["inbox_duplicates"], 1)
        self.assertEqual(len(self.events()), 1)

    def test_legacy_pulse_ping_drained(self):
        p = self.inbox / "pulse-1751234567.json"
        p.write_text("{}")
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 1)
        self.assertFalse(p.exists())
        self.assertEqual(self.events()[0]["source"], "pulse")

    def test_missing_inbox_dir_is_noop(self):
        (self.home / ".assistant/inbox").rmdir()
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 0)
        self.assertFalse(s["locked"])


# ─── replay safety: unlink only after append ────────────────────────────────

class ReplaySafetyTests(SpineTestCase):
    def test_kill_between_archive_and_append_is_replay_safe(self):
        p = self.drop_signal()
        with mock.patch.object(eventspine, "_append_event",
                               side_effect=OSError("simulated kill")):
            s1 = eventspine.drain_typed_inbox()
        # Nothing appended, nothing lost: the drop is still in the inbox.
        self.assertEqual(s1["inbox_deferred"], 1)
        self.assertEqual(s1["events_appended"], 0)
        self.assertTrue(p.exists(), "drop must never be unlinked before append")
        self.assertEqual(self.events(), [])
        # The next drain replays it cleanly — exactly one row, then unlink.
        s2 = eventspine.drain_typed_inbox()
        self.assertEqual(s2["events_appended"], 1)
        self.assertFalse(p.exists())
        self.assertEqual(len(self.events()), 1)


# ─── consumer lock ──────────────────────────────────────────────────────────

class LockTests(SpineTestCase):
    def test_two_consumers_armed_exactly_one_drains(self):
        p = self.drop_signal()
        # Consumer A holds the lock (live pid — our own).
        self.assertTrue(eventspine.acquire_consumer_lock())
        # Consumer B arms and must yield: nothing consumed, inbox untouched.
        s = eventspine.drain_typed_inbox()
        self.assertTrue(s["locked"])
        self.assertEqual(s["events_appended"], 0)
        self.assertTrue(p.exists())
        # A releases; the next drain does the work.
        eventspine.release_consumer_lock()
        s2 = eventspine.drain_typed_inbox()
        self.assertFalse(s2["locked"])
        self.assertEqual(s2["events_appended"], 1)

    def test_acquire_is_exclusive_while_held(self):
        self.assertTrue(eventspine.acquire_consumer_lock())
        try:
            self.assertFalse(eventspine.acquire_consumer_lock())
        finally:
            eventspine.release_consumer_lock()

    def test_stale_lock_from_dead_pid_is_reclaimed(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()  # reaped → the pid is positively dead
        lock = eventspine.lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": proc.pid, "ts": "x"}))
        p = self.drop_signal()
        s = eventspine.drain_typed_inbox()
        self.assertFalse(s["locked"])
        self.assertEqual(s["events_appended"], 1)
        self.assertFalse(p.exists())
        # Lock released after the drain.
        self.assertFalse(lock.exists())

    def test_garbage_lock_content_is_reclaimed(self):
        lock = eventspine.lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not json")
        self.drop_signal()
        s = eventspine.drain_typed_inbox()
        self.assertFalse(s["locked"])
        self.assertEqual(s["events_appended"], 1)

    def test_release_never_clobbers_another_consumers_lock(self):
        lock = eventspine.lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 1, "ts": "x"}))  # launchd: alive
        eventspine.release_consumer_lock()
        self.assertTrue(lock.exists())


# ─── fleet-as-connector: orphaned crash events ──────────────────────────────

class CrashEventTests(SpineTestCase):
    def crash_drop(self, name="workspace-5-1712345678.json", **overrides) -> Path:
        cdir = self.home / ".claude/cmux-crash-events"
        cdir.mkdir(parents=True, exist_ok=True)
        body = {"schema_version": 2, "workspace_ref": "workspace:5",
                "name": "My WS", "cause": "crash", "resume": None,
                "died_at": "2026-07-09T12:00:00Z"}
        body.update(overrides)
        p = cdir / name
        p.write_text(json.dumps(body, indent=2))
        return p

    def test_orphaned_crash_event_consumed_but_never_unlinked(self):
        p = self.crash_drop()
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["crash_appended"], 1)
        self.assertTrue(p.exists(), "crash-events dir is not ours to empty")
        ev = self.events()[0]
        self.assertEqual(ev["external_id"], "cmux:workspace:5:closed:1712345678")
        self.assertEqual(ev["refs"]["ws_ref"], "workspace:5")
        # Exactly once across rescans of the still-present file.
        s2 = eventspine.drain_typed_inbox()
        self.assertEqual(s2["crash_appended"], 0)
        self.assertEqual(len(self.events()), 1)

    def test_malformed_crash_event_ledgered_once_and_left_in_place(self):
        cdir = self.home / ".claude/cmux-crash-events"
        cdir.mkdir(parents=True, exist_ok=True)
        bad = cdir / "workspace-9-1712345679.json"
        bad.write_text("{ broken")
        eventspine.drain_typed_inbox()
        eventspine.drain_typed_inbox()
        self.assertTrue(bad.exists())
        # One ledger row, not one per pulse.
        self.assertEqual(len(self.ledger_rows("eventspine-crash-skip")), 1)

    def test_crash_events_older_than_dedup_window_are_skipped(self):
        p = self.crash_drop()
        old = time.time() - eventspine.DEDUP_RETENTION_SEC - 1000
        os.utime(p, (old, old))
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["crash_appended"], 0)


# ─── ws_ref event-priority promotion in pick-ws-batch ───────────────────────

class PickWsBatchPromotionTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        (self.home / ".assistant/observer-summaries").mkdir(parents=True)
        self.mod = load_script(self.home, "pick-ws-batch.py")

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def seed_summary(self, ws_ref, ts):
        p = (self.home / ".assistant/observer-summaries"
             / (ws_ref.replace(":", "_") + ".json"))
        p.write_text(json.dumps({"last_updated_ts": ts}))

    def seed_event(self, ws_ref, epoch):
        with open(self.home / ".assistant/events.jsonl", "a") as f:
            f.write(json.dumps({
                "schema": "world-event/1", "id": f"id-{ws_ref}-{epoch}",
                "ts": "2026-07-09T12:00:00Z", "epoch": epoch,
                "source": "cmux", "kind": "needs_input",
                "external_id": f"cmux:{ws_ref}:needs_input:{epoch}",
                "refs": {"ws_ref": ws_ref},
            }) + "\n")

    def run_main(self, ws_list):
        capture = io.StringIO()
        with mock.patch.object(self.mod.subprocess, "check_output",
                               return_value=json.dumps(ws_list)):
            with mock.patch("sys.stdout", capture):
                self.mod.main()
        return json.loads(capture.getvalue())

    WS = [{"ref": "workspace:1", "title": "one", "current_directory": "/a"},
          {"ref": "workspace:2", "title": "two", "current_directory": "/b"}]

    def test_lru_order_without_events(self):
        self.seed_summary("workspace:1", 1000)  # older summary → first
        self.seed_summary("workspace:2", 2000)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:1", "workspace:2"])

    def test_event_priority_beats_lru(self):
        # ws:2 was observed most recently (last in LRU order), but it carries
        # a WorldEvent newer than its summary → it jumps the queue.
        self.seed_summary("workspace:1", 1000)
        self.seed_summary("workspace:2", 2000)
        self.seed_event("workspace:2", 3000)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:2", "workspace:1"])
        self.assertTrue(out["to_reclassify"][0].get("event_priority"))
        self.assertNotIn("event_priority", out["to_reclassify"][1])

    def test_already_observed_event_does_not_promote(self):
        # The summary is NEWER than the event → the event was already seen by
        # an Observer pass; plain LRU applies.
        self.seed_summary("workspace:1", 1000)
        self.seed_summary("workspace:2", 2000)
        self.seed_event("workspace:2", 1500)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:1", "workspace:2"])

    def test_corrupt_events_log_falls_back_to_lru(self):
        self.seed_summary("workspace:1", 1000)
        self.seed_summary("workspace:2", 2000)
        (self.home / ".assistant/events.jsonl").write_text("{ not json\n")
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:1", "workspace:2"])


# ─── world.json events section ──────────────────────────────────────────────

class WorldScannerEventsTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        (self.home / ".assistant").mkdir(parents=True)
        self.mod = load_script(self.home, "world-scanner.py")

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def seed_events(self, rows):
        with open(self.home / ".assistant/events.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_counts_and_latest_age_per_source(self):
        now = self.mod.utc_now()
        now_epoch = now.timestamp()
        self.seed_events([
            {"source": "cmux", "kind": "needs_input",
             "ts": self.mod.iso(now), "epoch": int(now_epoch - 120)},
            {"source": "cmux", "kind": "work_complete",
             "ts": self.mod.iso(now), "epoch": int(now_epoch - 300)},
            # A source whose last event predates the 24h window: count_24h=0
            # but its (large) age is still reported — that's the stall signal.
            {"source": "pulse", "kind": "ping",
             "ts": self.mod.iso(now), "epoch": int(now_epoch - 90_000)},
        ])
        out = self.mod.build_events_summary(now)
        self.assertEqual(out["total_24h"], 2)
        self.assertEqual(out["by_source"]["cmux"]["count_24h"], 2)
        self.assertAlmostEqual(out["by_source"]["cmux"]["latest_age_sec"],
                               120, delta=5)
        self.assertEqual(out["by_source"]["pulse"]["count_24h"], 0)
        self.assertGreater(out["by_source"]["pulse"]["latest_age_sec"], 80_000)

    def test_quarantine_backlog_is_counted(self):
        qdir = self.home / ".assistant/eventspine/quarantine"
        qdir.mkdir(parents=True)
        (qdir / "1-bad.json").write_text("{")
        out = self.mod.build_events_summary(self.mod.utc_now())
        self.assertEqual(out["quarantine_pending"], 1)

    def test_missing_events_log_yields_empty_section(self):
        out = self.mod.build_events_summary(self.mod.utc_now())
        self.assertEqual(out, {"total_24h": 0, "by_source": {},
                               "quarantine_pending": 0})

    def test_build_exposes_events_section_in_world_json(self):
        now = self.mod.utc_now()
        self.seed_events([{"source": "cmux", "kind": "needs_input",
                           "ts": self.mod.iso(now),
                           "epoch": int(now.timestamp() - 60)}])
        with mock.patch.object(self.mod, "cmux_tree", lambda: None):
            with mock.patch.object(self.mod, "read_mem_pct", lambda: None):
                with mock.patch.object(self.mod, "ps_tty", lambda pid: None):
                    self.mod.build()
        world = json.loads(
            (self.home / ".claude/cache/world.json").read_text())
        self.assertIn("events", world)
        self.assertEqual(world["events"]["total_24h"], 1)
        self.assertEqual(world["counts"]["events_24h"], 1)
        self.assertIn("cmux", world["events"]["by_source"])


if __name__ == "__main__":
    unittest.main()
