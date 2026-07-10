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
        eventspine.release_consumer_lock()  # never leak a held flock across tests
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    # ── helpers ─────────────────────────────────────────────────────────

    def drop_signal(self, name="cmux-workspace-7-a.json", ws_ref="workspace:7",
                    signal="needs_input", ts="2026-07-09T12:00:00Z",
                    pattern="Notification", snippet="approve?") -> Path:
        """A cmux-watcher-shaped inbox drop (the real producer's schema).
        ws_ref=None mirrors the watcher's unresolved-workspace drops."""
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

    def test_distinct_content_same_bucket_stays_distinct(self):
        # Two genuinely different signals 8 min apart share the ts bucket but
        # differ in content → different external_ids. The watcher is
        # edge-triggered: a swallowed signal is never re-sent, so bucket-only
        # dedup would lose the second question forever.
        base = {"event": "workspace_signal", "ws_ref": "workspace:3",
                "signal_type": "needs_input", "pattern_matched": "Notification"}
        a = eventspine.normalize_inbox_item(
            "a.json", dict(base, ts="2026-07-09T12:01:00Z",
                           screen_snippet="Question ONE?"), 0)
        b = eventspine.normalize_inbox_item(
            "b.json", dict(base, ts="2026-07-09T12:09:00Z",
                           screen_snippet="Question TWO (different!)"), 0)
        self.assertNotEqual(a["external_id"], b["external_id"])

    def test_null_ws_ref_drops_do_not_collapse(self):
        # Two unresolved-workspace drops must not fuse into one "unknown"
        # identity — the filename stem keeps them apart.
        base = {"event": "workspace_signal", "ws_ref": None,
                "signal_type": "needs_input", "screen_snippet": "same"}
        a = eventspine.normalize_inbox_item(
            "cmux-unknown-1.json", dict(base, ts="2026-07-09T12:01:00Z"), 0)
        b = eventspine.normalize_inbox_item(
            "cmux-unknown-2.json", dict(base, ts="2026-07-09T12:02:00Z"), 0)
        self.assertNotEqual(a["external_id"], b["external_id"])
        self.assertIn("cmux-unknown-1", a["external_id"])

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

    def test_crash_event_without_identity_raises(self):
        # No filename epoch AND no parseable died_at → no stable identity.
        # Minting time.time() here meant one duplicate row per drain forever;
        # the caller must route these to the ledger-once skip path instead.
        with self.assertRaises(ValueError):
            eventspine.normalize_crash_event(
                Path("/x/workspace-9.json"),
                {"workspace_ref": "workspace:9", "cause": "crash"})


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
        # Same ws/signal/ts-bucket AND same content dropped twice (watcher
        # re-ping of one blocked state) → one row, both files disposed of.
        a = self.drop_signal(name="cmux-a.json", ts="2026-07-09T12:00:01Z")
        b = self.drop_signal(name="cmux-b.json", ts="2026-07-09T12:01:30Z")
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 1)
        self.assertEqual(s["inbox_duplicates"], 1)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(len(self.events()), 1)

    def test_distinct_signals_in_one_bucket_both_consumed(self):
        # Two different questions 8 min apart (same 10-min bucket) → 2 rows.
        self.drop_signal(name="cmux-a.json", ts="2026-07-09T12:01:00Z",
                         snippet="Question ONE?")
        self.drop_signal(name="cmux-b.json", ts="2026-07-09T12:09:00Z",
                         snippet="Question TWO (different!)")
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 2)
        self.assertEqual(s["inbox_duplicates"], 0)
        self.assertEqual(len(self.events()), 2)

    def test_null_ws_ref_distinct_files_both_consumed(self):
        self.drop_signal(name="cmux-unknown-a.json", ws_ref=None,
                         ts="2026-07-09T12:01:00Z")
        self.drop_signal(name="cmux-unknown-b.json", ws_ref=None,
                         ts="2026-07-09T12:02:00Z")
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 2)

    def test_duplicate_detected_across_drains_without_index(self):
        # Crash window: row appended but the dedup-index write never landed.
        # The tail scan of events.jsonl must still catch the replay — and the
        # duplicate sighting must BACKFILL the index, so the id is protected
        # by the full 30-day memory again, not just the 512KB tail window.
        self.drop_signal(name="cmux-a.json")
        eventspine.drain_typed_inbox()
        ev_id = self.events()[0]["id"]
        eventspine.dedup_index_path().unlink()
        self.drop_signal(name="cmux-a2.json")  # same bucket+content → same id
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["inbox_duplicates"], 1)
        self.assertEqual(len(self.events()), 1)
        index = json.loads(eventspine.dedup_index_path().read_text())
        self.assertIn(ev_id, index, "duplicate sighting must refresh the index")

    def test_torn_tail_repaired_before_append(self):
        # A crash mid-append leaves a partial line without a newline. The next
        # append must repair it, not glue on and corrupt both rows.
        ev_path = self.home / ".assistant/events.jsonl"
        ev_path.write_text('{"schema":"world-event/1","id":"aaaa')  # torn
        self.drop_signal()
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 1)
        lines = ev_path.read_text().splitlines()
        self.assertEqual(len(lines), 2, "repair newline must separate the rows")
        new_row = json.loads(lines[1])  # parses → not glued to the torn tail
        self.assertEqual(new_row["kind"], "needs_input")
        # …and the tail-scan dedup can see the new id again.
        self.assertIn(new_row["id"], eventspine._recent_event_ids(ev_path))

    def test_quarantine_failure_defers_file_and_drain_continues(self):
        # If quarantining itself fails (move AND copy fallback), the drain
        # must ledger it and keep going — never abort on the except path.
        bad = self.inbox / "cmux-broken.json"
        bad.write_text("{ not json at all")
        good = self.drop_signal()
        real_replace, real_copy2 = os.replace, eventspine.shutil.copy2

        def replace_fails_for_quarantine(src, dst, *a, **k):
            if "quarantine" in str(dst):
                raise OSError("simulated EXDEV")
            return real_replace(src, dst, *a, **k)

        def copy2_fails_for_quarantine(src, dst, *a, **k):
            if "quarantine" in str(dst):
                raise OSError("simulated ENOSPC")
            return real_copy2(src, dst, *a, **k)

        with mock.patch.object(eventspine.os, "replace",
                               side_effect=replace_fails_for_quarantine), \
             mock.patch.object(eventspine.shutil, "copy2",
                               side_effect=copy2_fails_for_quarantine):
            s = eventspine.drain_typed_inbox()
        self.assertEqual(s["events_appended"], 1, "good drop still consumed")
        self.assertEqual(s["inbox_quarantined"], 0)
        self.assertEqual(s["inbox_deferred"], 1)
        self.assertTrue(bad.exists(), "unquarantinable file left for retry")
        self.assertFalse(good.exists())
        self.assertEqual(len(self.ledger_rows("eventspine-quarantine")), 1)

    def test_duplicate_drop_is_not_archived(self):
        # raw/ must not accumulate copies of dedup-duplicates.
        self.drop_signal(name="cmux-a.json")
        eventspine.drain_typed_inbox()
        self.drop_signal(name="cmux-b.json")  # same id (bucket+content)
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["inbox_duplicates"], 1)
        archived = [p.name for p in eventspine.raw_archive_dir().rglob("*.json")]
        self.assertIn("cmux-a.json", archived)
        self.assertNotIn("cmux-b.json", archived)

    def test_raw_archive_day_dirs_pruned_after_30_days(self):
        old_dir = eventspine.raw_archive_dir() / "2020-01-01"
        old_dir.mkdir(parents=True)
        (old_dir / "x.json").write_text("{}")
        self.drop_signal()
        eventspine.drain_typed_inbox()
        self.assertFalse(old_dir.exists(), "expired raw day dir must be pruned")
        # Today's archive (just written by this drain) survives.
        self.assertTrue(any(eventspine.raw_archive_dir().rglob("*.json")))

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

# Child process that takes the flock via the real acquire path, reports, and
# (optionally) holds it until killed. argv: <src-path> [hold]
_LOCK_CHILD = """\
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from assistant import eventspine
ok = eventspine.acquire_consumer_lock()
print(json.dumps({"acquired": ok, "pid": os.getpid()}), flush=True)
if ok and "hold" in sys.argv[2:]:
    time.sleep(60)
"""


class LockTests(SpineTestCase):
    """The consumer lock is fcntl.flock on a persistent fd: the kernel
    releases it on ANY holder death (SIGKILL included), so there is no pid
    heuristic to fool and no reclaim path for two contenders to race."""

    def spawn_holder(self, *args):
        proc = subprocess.Popen(
            [sys.executable, "-c", _LOCK_CHILD, str(REPO / "src"), *args],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        line = json.loads(proc.stdout.readline())
        return proc, line

    def test_two_consumers_armed_exactly_one_drains(self):
        p = self.drop_signal()
        # Consumer A holds the lock (this process).
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

    def test_two_contenders_exactly_one_wins(self):
        proc, info = self.spawn_holder("hold")
        self.assertTrue(info["acquired"])
        # A live holder in another process → we must lose, without stealing.
        self.assertFalse(eventspine.acquire_consumer_lock())
        s = eventspine.drain_typed_inbox()
        self.assertTrue(s["locked"])

    def test_clean_holder_exit_releases_lock(self):
        proc, info = self.spawn_holder()  # acquires, prints, exits
        self.assertTrue(info["acquired"])
        proc.wait()
        p = self.drop_signal()
        s = eventspine.drain_typed_inbox()
        self.assertFalse(s["locked"])
        self.assertEqual(s["events_appended"], 1)
        self.assertFalse(p.exists())

    def test_sigkilled_holder_releases_lock(self):
        # The kernel drops the flock the instant the holder dies — a SIGKILLed
        # drain can never stall the spine (the old pid-file scheme could,
        # forever, when the dead pid got recycled to a long-lived process).
        proc, info = self.spawn_holder("hold")
        self.assertTrue(info["acquired"])
        self.assertFalse(eventspine.acquire_consumer_lock())
        proc.kill()  # SIGKILL: no cleanup code runs in the child
        proc.wait()
        p = self.drop_signal()
        s = eventspine.drain_typed_inbox()
        self.assertFalse(s["locked"])
        self.assertEqual(s["events_appended"], 1)
        self.assertFalse(p.exists())

    def test_lock_file_content_is_observability_only(self):
        # Garbage content / a scary-looking live pid in the file mean nothing:
        # only the kernel flock decides. No content can wedge the spine.
        lock = eventspine.lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 1, "ts": "2026-06-01T00:00:00Z"}))
        self.drop_signal()
        s = eventspine.drain_typed_inbox()
        self.assertFalse(s["locked"])
        self.assertEqual(s["events_appended"], 1)
        lock.write_text("not json")
        self.drop_signal(name="cmux-second.json", ts="2026-07-09T13:00:00Z")
        s2 = eventspine.drain_typed_inbox()
        self.assertFalse(s2["locked"])

    def test_release_without_ownership_never_disturbs_holder(self):
        proc, info = self.spawn_holder("hold")
        self.assertTrue(info["acquired"])
        eventspine.release_consumer_lock()  # we hold nothing — must be a no-op
        self.assertFalse(eventspine.acquire_consumer_lock())
        self.assertTrue(eventspine.lock_path().exists())


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

    def age_file(self, p: Path, sec: float) -> None:
        old = time.time() - sec
        os.utime(p, (old, old))

    def test_malformed_crash_event_ledgered_once_and_left_in_place(self):
        cdir = self.home / ".claude/cmux-crash-events"
        cdir.mkdir(parents=True, exist_ok=True)
        bad = cdir / "workspace-9-1712345679.json"
        bad.write_text("{ broken")
        self.age_file(bad, eventspine.CRASH_SKIP_GRACE_SEC + 60)  # persistent
        eventspine.drain_typed_inbox()
        eventspine.drain_typed_inbox()
        self.assertTrue(bad.exists())
        # One ledger row, not one per pulse.
        self.assertEqual(len(self.ledger_rows("eventspine-crash-skip")), 1)

    def test_fresh_unparseable_crash_file_skipped_silently(self):
        # workspace-watcher rewrites these files non-atomically: a fresh
        # parse failure is usually a healthy file caught mid-write. No
        # ledger noise until it stays broken past the grace window.
        cdir = self.home / ".claude/cmux-crash-events"
        cdir.mkdir(parents=True, exist_ok=True)
        bad = cdir / "workspace-9-1712345679.json"
        bad.write_text("{ mid-rewrite")
        s = eventspine.drain_typed_inbox()
        self.assertEqual(s["crash_appended"], 0)
        self.assertEqual(self.ledger_rows("eventspine-crash-skip"), [])
        # Still broken after the grace window → NOW it earns its one row.
        self.age_file(bad, eventspine.CRASH_SKIP_GRACE_SEC + 60)
        eventspine.drain_typed_inbox()
        self.assertEqual(len(self.ledger_rows("eventspine-crash-skip")), 1)

    def test_crash_file_without_identity_never_mints_time_ids(self):
        # No filename epoch, no died_at → previously a fresh time.time()
        # identity per drain = one duplicate row per pulse, forever.
        cdir = self.home / ".claude/cmux-crash-events"
        cdir.mkdir(parents=True, exist_ok=True)
        p = cdir / "workspace-9.json"
        p.write_text(json.dumps({"workspace_ref": "workspace:9",
                                 "cause": "crash"}))
        self.age_file(p, eventspine.CRASH_SKIP_GRACE_SEC + 60)
        for _ in range(3):
            s = eventspine.drain_typed_inbox()
            self.assertEqual(s["crash_appended"], 0)
        self.assertEqual(self.events(), [])
        self.assertTrue(p.exists())
        # Ledgered once via the skip path — visible, but never a row storm.
        self.assertEqual(len(self.ledger_rows("eventspine-crash-skip")), 1)

    def test_duplicate_crash_sighting_backfills_index(self):
        # Crash files are never unlinked: once the row leaves the events tail
        # window, only the index protects against a re-append. A duplicate
        # sighting must therefore refresh the index entry.
        self.crash_drop()
        eventspine.drain_typed_inbox()
        ev_id = self.events()[0]["id"]
        eventspine.dedup_index_path().unlink()  # index write "never landed"
        s2 = eventspine.drain_typed_inbox()  # tail scan catches the dup…
        self.assertEqual(s2["crash_appended"], 0)
        index = json.loads(eventspine.dedup_index_path().read_text())
        self.assertIn(ev_id, index, "…and must backfill the index")
        # Even with the tail scan blinded (rollover), no duplicate row.
        with mock.patch.object(eventspine, "_recent_event_ids",
                               return_value=set()):
            s3 = eventspine.drain_typed_inbox()
        self.assertEqual(s3["crash_appended"], 0)
        self.assertEqual(len(self.events()), 1)

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
        now = int(time.time())
        self.seed_summary("workspace:1", now - 3600)
        self.seed_summary("workspace:2", now - 1800)
        self.seed_event("workspace:2", now - 60)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:2", "workspace:1"])
        self.assertTrue(out["to_reclassify"][0].get("event_priority"))
        self.assertNotIn("event_priority", out["to_reclassify"][1])

    def test_already_observed_event_does_not_promote(self):
        # The summary is NEWER than the event → the event was already seen by
        # an Observer pass; plain LRU applies.
        now = int(time.time())
        self.seed_summary("workspace:1", now - 7200)
        self.seed_summary("workspace:2", now - 3600)
        self.seed_event("workspace:2", now - 5000)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:1", "workspace:2"])

    def test_promotions_capped_to_reserve_one_lru_slot(self):
        # 6 promoted candidates + 1 starving pure-LRU ws → promoted may take
        # at most BATCH_SIZE-1 slots; the last slot goes to the LRU ws. This
        # is the anti-starvation guarantee: chatty blocked workspaces can
        # never fill the whole batch every pulse.
        now = int(time.time())
        ws = [{"ref": f"workspace:{i}", "title": f"w{i}",
               "current_directory": "/x"} for i in range(1, 8)]
        for i in range(1, 7):  # ws1..ws6: fresh events, hour-old summaries
            self.seed_summary(f"workspace:{i}", now - 3600)
            self.seed_event(f"workspace:{i}", now - 100 + i)
        self.seed_summary("workspace:7", now - 2 * 86400)  # LRU rank 1
        out = self.run_main(ws)
        batch = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(len(batch), self.mod.BATCH_SIZE)
        self.assertIn("workspace:7", batch, "reserved LRU slot")
        promoted_in_batch = [w for w in out["to_reclassify"]
                             if w.get("event_priority")]
        self.assertEqual(len(promoted_in_batch), self.mod.BATCH_SIZE - 1)
        # The two overflow promoted candidates wait in reuse_cached.
        self.assertEqual(len(out["reuse_cached"]), 2)

    def test_promotion_cooldown_skips_just_observed_ws(self):
        # ws:2 was in the previous batch (summary refreshed minutes ago); a
        # newer event must NOT re-promote it until the cooldown lapses —
        # otherwise a chatty blocked ws re-promotes every single pulse.
        now = int(time.time())
        self.seed_summary("workspace:1", now - 3600)
        self.seed_summary("workspace:2", now - 120)  # just observed
        self.seed_event("workspace:2", now - 60)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:1", "workspace:2"])
        self.assertNotIn("event_priority", out["to_reclassify"][1])

    def test_stale_event_does_not_promote(self):
        # An event older than 24h is history, not a signal — plain LRU.
        now = int(time.time())
        self.seed_summary("workspace:1", now - 3600)
        self.seed_summary("workspace:2", now - 40 * 3600)
        self.seed_event("workspace:2", now - 30 * 3600)  # newer than summary…
        out = self.run_main(self.WS)
        # …but too old to promote: ws:2 leads on LRU age, without the flag.
        self.assertNotIn("event_priority", out["to_reclassify"][0])
        self.assertNotIn("event_priority", out["to_reclassify"][1])

    def test_back_off_beats_promotion(self):
        # A backed-off ws never enters the batch, fresh event or not.
        now = int(time.time())
        with open(self.home / ".assistant/back-off.json", "w") as f:
            json.dump({"workspaces": [
                {"ws_ref": "workspace:2", "reason": "loop"}]}, f)
        self.seed_summary("workspace:1", now - 3600)
        self.seed_summary("workspace:2", now - 7200)
        self.seed_event("workspace:2", now - 60)
        out = self.run_main(self.WS)
        refs = [w["ref"] for w in out["to_reclassify"]]
        self.assertEqual(refs, ["workspace:1"])
        self.assertEqual([b["ref"] for b in out["backed_off"]],
                         ["workspace:2"])

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
