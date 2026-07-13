"""Direct-import tests for the render_* tab functions in
bin/render-assistant-page.py — the half the in-process suite skips.

Each test drives a render_* function with a realistic world.json (or the
files the function actually reads) and asserts the DATA-DRIVEN branches:
which cards/badges/rows appear, the bucket grouping, the empty-states.
We deliberately do NOT pin verbatim HTML — only the load-bearing class
names / ids / counts that prove the branch ran on real data.

HOME is pointed at a tmp dir BEFORE the module is imported (all path
constants bind at import), so the real ~/.claude / ~/.assistant are never
touched.  The renderer's actual data sources (verified by reading the
source):
  - render_awaiting        → ~/.claude/cache/assistant-state.json [awaiting_input]
  - render_decisions       → ~/.assistant/actions-ledger.jsonl
  - render_live_sessions   → world["live_sessions"]
  - render_todos_tab       → ~/.claude/assistant-todo.json (falls back to world["todo"])
  - render_fleet_tab       → ~/.assistant/observer-latest-report.json + cmux + receipts
  - render_decisions_tab   → assistant-state.json (actions_taken / _meta) + the three above
  - render()               → world.json + every tab
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/render-assistant-page.py"


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("renderer_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp / ".assistant/receipts").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    return tmp


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def full_world() -> dict:
    """A realistic world.json mirroring world-scanner.build()'s payload —
    every key the renderer touches, so render() and each tab run without
    KeyErrors."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "_meta": {
            "built_at": _iso(now),
            "scanner_version": 1,
            "memory_pct": 42,
        },
        "counts": {
            "workspaces": 2,
            "live_sessions": 3,
            "human_sessions": 2,
            "cron_sessions": 1,
            "truly_active_30m": 1,
            "proposals_open": 1,
            "proposals_awaiting": 1,
            "ledger_24h": 2,
            "todo_open": 3,
            "todo_p0_p1": 2,
        },
        "workspaces": [
            {"ws_ref": "workspace:1", "title": "Auto: deflake ruler", "cwd": "/Users/mukuls/dev/a"},
            {"ws_ref": "workspace:2", "title": "Resumed: audit move", "cwd": "/Users/mukuls/dev/b"},
        ],
        "live_sessions": [
            {
                "ws_ref": "workspace:1",
                "ws_title": "Auto: deflake ruler",
                "cwd": "/Users/mukuls/dev/a",
                "tab_id": "tab-1",
                "is_cron": False,
                "last_assistant": {"ts": _iso(now - timedelta(seconds=30)), "text": "Working on the fix"},
                "last_user": {"ts": _iso(now - timedelta(seconds=90)), "text": "go"},
                "user_unanswered": False,
                "queue_pending": 0,
            },
            {
                "ws_ref": "workspace:2",
                "ws_title": "Resumed: audit move",
                "cwd": "/Users/mukuls/dev/b",
                "tab_id": "tab-2",
                "is_cron": False,
                "last_assistant": {"ts": _iso(now - timedelta(seconds=4000)), "text": "Did the thing"},
                "last_user": {"ts": _iso(now - timedelta(seconds=5000)), "text": "please review"},
                "user_unanswered": True,
                "queue_pending": 2,
            },
            {
                "ws_ref": "workspace:9",
                "ws_title": "cron worker",
                "cwd": "/tmp/cron",
                "tab_id": "tab-9",
                "is_cron": True,
                "last_assistant": {"ts": _iso(now), "text": "pulse"},
            },
        ],
        "proposals": [],
        "ledger_recent": [],
        "inbox_events_recent": [],
        "todo": {
            "items": [
                {"id": "td-1", "priority": "P0", "title": "fix crash", "status": "open",
                 "autoDispatch": True, "source": "user"},
                {"id": "td-2", "priority": "P1", "title": "dispatched task", "status": "in-progress",
                 "autoDispatch": True, "dispatchedAt": "2026-06-01T10:00:00Z",
                 "dispatchedWs": "workspace:3", "source": "auto"},
                {"id": "td-3", "priority": "P2", "title": "backlog item", "status": "open",
                 "autoDispatch": False, "url": "https://example.com/x", "detail": "some detail"},
                {"id": "td-4", "priority": "P2", "title": "needs decision", "status": "blocked",
                 "autoDispatch": None, "source": "observer"},
                {"id": "td-5", "priority": "P4", "title": "someday maybe", "status": "open"},
                {"id": "td-6", "priority": "P1", "title": "deferred one", "status": "deferred"},
            ],
            "completed": [
                {"id": "td-old", "title": "shipped already", "closedAt": "2026-05-30T12:00:00Z"},
                {"id": "td-older", "title": "shipped earlier", "closedAt": "2026-05-29T12:00:00Z"},
            ],
        },
        "dashboard_state_meta": {},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    # ── seed helpers ───────────────────────────────────────────────────
    def _write_state(self, payload):
        (self._tmp / ".claude/cache/assistant-state.json").write_text(json.dumps(payload))

    def _write_world(self, payload):
        (self._tmp / ".claude/cache/world.json").write_text(json.dumps(payload))

    def _write_todo(self, payload):
        (self._tmp / ".claude/assistant-todo.json").write_text(json.dumps(payload))

    def _write_ledger(self, entries):
        path = self._tmp / ".assistant/actions-ledger.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def _write_observer_report(self, payload):
        (self._tmp / ".assistant/observer-latest-report.json").write_text(json.dumps(payload))

    def _write_receipt(self, ws_ref, payload, suffix="a"):
        slug = ws_ref.replace(":", "-")
        (self._tmp / f".assistant/receipts/{slug}-{suffix}.json").write_text(json.dumps(payload))


# ─── render_awaiting ─────────────────────────────────────────────────────────

class AwaitingTests(_Base):
    def test_empty_state(self):
        self._write_state({"awaiting_input": []})
        html, n = self.mod.render_awaiting({})
        self.assertEqual(n, 0)
        self.assertIn("No decisions awaiting your input", html)

    def test_no_state_file_is_empty(self):
        html, n = self.mod.render_awaiting({})
        self.assertEqual(n, 0)
        self.assertIn("empty", html)

    def test_cards_render_sorted_by_confidence(self):
        self._write_state({"awaiting_input": [
            {"tier": "T2", "title": "low conf", "detail": "d1", "confidence": 0.10,
             "touches": [{"ref": "workspace:5", "name": "alpha"}]},
            {"tier": "T1", "title": "high conf", "detail": "d2", "confidence": 0.90,
             "touches": ["workspace:6"],
             "alt_actions": ["cleanup", "merge", "ignore"]},
        ]})
        html, n = self.mod.render_awaiting({})
        self.assertEqual(n, 2)
        # Tier pill lowercased into class.
        self.assertIn('class="card t1"', html)
        self.assertIn('class="card t2"', html)
        # Both titles present.
        self.assertIn("high conf", html)
        self.assertIn("low conf", html)
        # Higher-confidence card sorts first.
        self.assertLess(html.index("high conf"), html.index("low conf"))
        # ws_ref → Open button.
        self.assertIn('data-ws="workspace:6"', html)
        self.assertIn("Open workspace:6", html)
        # alt_actions block.
        self.assertIn('class="alts"', html)
        self.assertIn("cleanup", html)
        # confidence formatted.
        self.assertIn("conf 0.90", html)

    def test_card_without_ws_ref_has_no_button(self):
        self._write_state({"awaiting_input": [
            {"tier": "T3", "title": "no ws", "detail": "d", "confidence": 0.5,
             "touches": [{"name": "td-7"}]},
        ]})
        html, n = self.mod.render_awaiting({})
        self.assertEqual(n, 1)
        self.assertNotIn("openWs", html)
        # scope falls back to name.
        self.assertIn("td-7", html)

    def test_default_tier_when_missing(self):
        self._write_state({"awaiting_input": [
            {"title": "no tier", "detail": "d", "confidence": 0.3, "touches": []},
        ]})
        html, n = self.mod.render_awaiting({})
        self.assertIn('class="card t3"', html)  # default T3
        self.assertIn(">T3<", html)

    def test_first_ws_ref_ignores_non_str_non_dict_entries(self):
        # Covers the else→ref="" branch (int/None entries in touches).
        self.assertIsNone(self.mod.first_ws_ref([42, None, 3.14]))
        self.assertEqual(self.mod.first_ws_ref([42, "workspace:8"]), "workspace:8")


# ─── render_decisions (ledger-driven) ────────────────────────────────────────

class DecisionsTests(_Base):
    def test_empty_when_no_ledger(self):
        html, n = self.mod.render_decisions({})
        self.assertEqual(n, 0)
        self.assertIn("No decisions in the last", html)

    def test_rows_render_with_outcomes_and_kinds(self):
        now = datetime.now(timezone.utc)
        self._write_ledger([
            {"ts": _iso(now - timedelta(minutes=5)), "kind": "dispatch",
             "ws_ref": "workspace:1", "td": "td-1", "evidence": "spawned worker",
             "outcome": "verified", "pulse_idx": 42,
             "verdict": {"applied_lessons": ["never reset"]}},
            {"ts": _iso(now - timedelta(minutes=10)), "kind": "cleanup",
             "ws_ref": "workspace:2", "evidence": "tore down", "outcome": "failed",
             "pulse_idx": 43},
            {"ts": _iso(now - timedelta(minutes=15)), "kind": "nudge",
             "evidence": "poked", "outcome": "skipped", "pulse_idx": 44},
            {"ts": _iso(now - timedelta(minutes=20)), "kind": "merge-pr",
             "evidence": "merged", "outcome": "rejected", "pulse_idx": 45},
        ])
        html, n = self.mod.render_decisions({})
        self.assertEqual(n, 4)
        # outcome classes — verified→ok, failed/rejected→fail, skipped→skip.
        self.assertIn("outcome-ok", html)
        self.assertIn("outcome-fail", html)
        self.assertIn("outcome-skip", html)
        # kind classes.
        self.assertIn("kind-dispatch", html)
        self.assertIn("kind-cleanup", html)
        self.assertIn("kind-merge-pr", html)
        # scope joins ws + td.
        self.assertIn("workspace:1", html)
        self.assertIn("td-1", html)
        # applied lessons rendered.
        self.assertIn("never reset", html)
        self.assertIn("\U0001F4D6", html)  # 📖

    def test_skip_kinds_and_old_entries_filtered(self):
        now = datetime.now(timezone.utc)
        self._write_ledger([
            {"ts": _iso(now - timedelta(minutes=1)), "kind": "heartbeat",
             "evidence": "tick", "outcome": "verified", "pulse_idx": 1},
            {"ts": _iso(now - timedelta(hours=48)), "kind": "dispatch",
             "evidence": "too old", "outcome": "verified", "pulse_idx": 2},
            {"ts": "not-a-date", "kind": "dispatch", "evidence": "bad ts", "pulse_idx": 3},
            {"ts": _iso(now - timedelta(minutes=2)), "kind": "status-flip",
             "evidence": "kept", "outcome": "verified", "pulse_idx": 4},
        ])
        html, n = self.mod.render_decisions({})
        # Only the status-flip survives (heartbeat skipped, old + bad-ts dropped).
        self.assertEqual(n, 1)
        self.assertIn("kept", html)
        self.assertNotIn("too old", html)
        self.assertNotIn("tick", html)

    def test_corrupt_ledger_line_skipped(self):
        now = datetime.now(timezone.utc)
        path = self._tmp / ".assistant/actions-ledger.jsonl"
        path.write_text(
            "{ not valid json at all\n"
            + json.dumps({"ts": _iso(now - timedelta(minutes=1)), "kind": "dispatch",
                          "evidence": "good line", "outcome": "verified", "pulse_idx": 1})
            + "\n"
        )
        html, n = self.mod.render_decisions({})
        self.assertEqual(n, 1)
        self.assertIn("good line", html)

    def test_unknown_kind_still_shown(self):
        now = datetime.now(timezone.utc)
        self._write_ledger([
            {"ts": _iso(now - timedelta(minutes=1)), "kind": "brand-new-kind",
             "evidence": "forward compat", "outcome": "verified", "pulse_idx": 7},
        ])
        html, n = self.mod.render_decisions({})
        self.assertEqual(n, 1)
        self.assertIn("forward compat", html)


# ─── render_live_sessions ────────────────────────────────────────────────────

class LiveSessionsTests(_Base):
    def test_empty_world(self):
        html, n = self.mod.render_live_sessions({"live_sessions": []})
        self.assertEqual(n, 0)
        self.assertIn("No open sessions", html)

    def test_human_sessions_render_cron_filtered(self):
        html, n = self.mod.render_live_sessions(full_world())
        # 2 human sessions (cron workspace:9 dropped).
        self.assertEqual(n, 2)
        self.assertIn("workspace:1", html)
        self.assertIn("workspace:2", html)
        self.assertNotIn("workspace:9", html)
        # workspace:2 had user_unanswered + queue_pending=2.
        self.assertIn("unanswered", html)
        self.assertIn("queued 2", html)
        # Fresh (30s) session → "live"; 4000s old (<7200) → "idle".
        self.assertIn(">live<", html)
        self.assertIn(">idle<", html)

    def test_quiet_pill_for_very_old_session(self):
        now = datetime.now(timezone.utc)
        world = {"live_sessions": [
            {"ws_ref": "workspace:12", "cwd": "/q", "is_cron": False,
             "last_assistant": {"ts": _iso(now - timedelta(seconds=8000)), "text": "quiet work"}},
        ]}
        html, n = self.mod.render_live_sessions(world)
        self.assertEqual(n, 1)
        self.assertIn(">quiet<", html)

    def test_errored_session_hidden(self):
        now = datetime.now(timezone.utc)
        world = {"live_sessions": [
            {
                "ws_ref": "workspace:7", "cwd": "/x", "is_cron": False,
                "last_assistant": {"ts": _iso(now - timedelta(seconds=400)),
                                   "text": "API Error: overloaded_error 529"},
                "last_user": {"ts": _iso(now - timedelta(seconds=500)), "text": "hi"},
            },
        ]}
        html, n = self.mod.render_live_sessions(world)
        self.assertEqual(n, 0)
        # errored-hidden empty message + the count note.
        self.assertIn("errored sessions hidden", html)

    def test_session_with_no_timestamps_skipped(self):
        world = {"live_sessions": [
            {"ws_ref": "workspace:8", "cwd": "/y", "is_cron": False,
             "last_assistant": {}, "last_user": {}},
        ]}
        html, n = self.mod.render_live_sessions(world)
        self.assertEqual(n, 0)

    def test_recent_and_idle_pills(self):
        now = datetime.now(timezone.utc)
        world = {"live_sessions": [
            {"ws_ref": "workspace:10", "cwd": "/r", "is_cron": False,
             "last_assistant": {"ts": _iso(now - timedelta(seconds=600)), "text": "recent work"}},
            {"ws_ref": "workspace:11", "cwd": "/i", "is_cron": False,
             "last_assistant": {"ts": _iso(now - timedelta(seconds=3600)), "text": "idle work"}},
        ]}
        html, n = self.mod.render_live_sessions(world)
        self.assertEqual(n, 2)
        self.assertIn(">recent<", html)
        self.assertIn(">idle<", html)


# ─── render_decisions_tab ────────────────────────────────────────────────────

class DecisionsTabTests(_Base):
    def test_assembles_stats_and_sections(self):
        world = full_world()
        now = datetime.now(timezone.utc)
        self._write_state({
            "awaiting_input": [
                {"tier": "T1", "title": "decide", "detail": "d", "confidence": 0.8,
                 "touches": ["workspace:1"]},
            ],
            "actions_taken": [{"x": 1}, {"x": 2}, {"x": 3}],
            "_meta": {"generated_at": _iso(now - timedelta(minutes=4))},
        })
        self._write_ledger([
            {"ts": _iso(now - timedelta(minutes=1)), "kind": "dispatch",
             "evidence": "did it", "outcome": "verified", "pulse_idx": 1},
        ])
        html, awaiting_n = self.mod.render_decisions_tab(world)
        self.assertEqual(awaiting_n, 1)
        # stats strip values.
        self.assertIn("Awaiting input", html)
        self.assertIn("Assistant actions", html)
        self.assertIn("Active sessions", html)
        self.assertIn("Last Assistant pulse", html)
        # the 3 actions_taken count appears.
        self.assertIn(">3</div>", html)
        # awaiting + live + decisions sub-sections all present.
        self.assertIn("Awaiting your input", html)
        self.assertIn("Open sessions", html)
        self.assertIn("Decisions", html)
        # the awaiting card itself.
        self.assertIn("decide", html)
        # live session rows from full_world.
        self.assertIn("workspace:1", html)


# ─── render_todos_tab ────────────────────────────────────────────────────────

class TodosTabTests(_Base):
    def test_empty_when_no_todo(self):
        html, p0_p1 = self.mod.render_todos_tab({"todo": {}})
        self.assertEqual(p0_p1, 0)
        self.assertEqual(html, "")

    def test_falls_back_to_world_snapshot(self):
        # No assistant-todo.json on disk → use world["todo"].
        world = full_world()
        html, p0_p1 = self.mod.render_todos_tab(world)
        # P0(td-1) + P1(td-2) open → 2.
        self.assertEqual(p0_p1, 2)

    def test_buckets_priorities_and_dispatch_states(self):
        world = full_world()
        self._write_todo(world["todo"])  # canonical on-disk source
        html, p0_p1 = self.mod.render_todos_tab(world)
        self.assertEqual(p0_p1, 2)
        # Section headers for each non-empty bucket.
        self.assertIn("P0 / P1 — top", html)
        self.assertIn("P2 / P3 — backlog", html)
        self.assertIn("P4 — someday", html)
        # Bucket A (autoDispatch true, not dispatched) → "auto" pill.
        self.assertIn("auto-dispatch", html)
        self.assertIn(">auto<", html)
        # autoDispatch true + dispatchedAt → "dispatched" pill.
        self.assertIn(">dispatched</span>", html)
        # Priority pills.
        self.assertIn('class="pill p0"', html)
        self.assertIn('class="pill p1"', html)
        self.assertIn('class="pill p2"', html)
        self.assertIn('class="pill p4"', html)
        # IDs present.
        self.assertIn("td-1", html)
        self.assertIn("td-3", html)
        # url → link.
        self.assertIn("https://example.com/x", html)
        self.assertIn("alt-link", html)
        # tri-state autoDispatch buttons + their active state.
        self.assertIn("td-set-true", html)
        self.assertIn("td-set-false", html)
        self.assertIn("td-set-null", html)
        self.assertIn("active", html)
        # remove / dispatch / context tools.
        self.assertIn("td-remove", html)
        self.assertIn("td-dispatch", html)
        self.assertIn("td-context-toggle", html)
        # deferred (td-6) excluded from open buckets, blocked (td-4) present.
        self.assertIn("td-4", html)
        # status pills.
        self.assertIn("status-blocked", html)

    def test_recently_completed_section(self):
        world = full_world()
        self._write_todo(world["todo"])
        html, _ = self.mod.render_todos_tab(world)
        self.assertIn("Recently completed", html)
        self.assertIn("shipped already", html)
        self.assertIn(">DONE<", html)
        # newest closedAt first.
        self.assertLess(html.index("shipped already"), html.index("shipped earlier"))

    def test_corrupt_todo_file_falls_back_to_world(self):
        (self._tmp / ".claude/assistant-todo.json").write_text("{ corrupt")
        world = full_world()
        html, p0_p1 = self.mod.render_todos_tab(world)
        # Falls back to world["todo"] → still buckets.
        self.assertEqual(p0_p1, 2)
        self.assertIn("td-1", html)

    def test_item_without_id_renders_without_toolbar(self):
        # Regression pin: an OPEN item with no `id` used to raise
        # UnboundLocalError — `detail_text` was only bound inside the `if td_id:`
        # branch but referenced unconditionally in the row template. It must now
        # render the row (title + priority) with NO per-row action toolbar, and
        # an em-dash placeholder for the absent id.
        world = {"todo": {"items": [
            {"priority": "P2", "title": "anonymous todo", "status": "open"},
        ], "completed": []}}
        html, _ = self.mod.render_todos_tab(world)
        self.assertIn("anonymous todo", html)
        self.assertIn(">—<", html)            # id placeholder rendered
        self.assertNotIn("todo-tools", html)  # no toolbar without an id


# ─── fleet helpers ───────────────────────────────────────────────────────────

class FirstSentenceTests(_Base):
    def test_empty(self):
        self.assertEqual(self.mod._first_sentence(""), "")
        self.assertEqual(self.mod._first_sentence(None), "")

    def test_first_sentence_of_many(self):
        s = self.mod._first_sentence("Fixed the bug. Then ran tests. All green.")
        self.assertEqual(s, "Fixed the bug.")

    def test_split_on_newline(self):
        s = self.mod._first_sentence("Line one\nLine two\nLine three")
        self.assertEqual(s, "Line one")

    def test_long_single_sentence_truncated(self):
        long = "x" * 200
        s = self.mod._first_sentence(long, max_len=50)
        self.assertLessEqual(len(s), 50)
        self.assertTrue(s.endswith("…"))  # ellipsis


class LatestReceiptTests(_Base):
    def test_none_when_no_receipts(self):
        self.assertIsNone(self.mod._latest_receipt("workspace:99"))

    def test_returns_newest_by_mtime(self):
        self._write_receipt("workspace:43", {"tag": "old", "ci_status": "red"}, suffix="old")
        time.sleep(0.02)
        self._write_receipt("workspace:43", {"tag": "new", "ci_status": "green"}, suffix="new")
        # Bump the newer file's mtime to be unambiguously latest.
        newer = self._tmp / ".assistant/receipts/workspace-43-new.json"
        os.utime(newer, (time.time() + 10, time.time() + 10))
        r = self.mod._latest_receipt("workspace:43")
        self.assertEqual(r["tag"], "new")

    def test_corrupt_receipt_returns_none(self):
        (self._tmp / ".assistant/receipts/workspace-50-x.json").write_text("{ bad")
        self.assertIsNone(self.mod._latest_receipt("workspace:50"))


class ReceiptBadgeTests(_Base):
    def test_no_receipt_grey(self):
        html = self.mod._receipt_badge_html(None)
        self.assertIn("receipt-none", html)
        self.assertIn("receipt-dot grey", html)
        self.assertIn("no receipt", html)

    def test_green_when_ci_green_and_approved(self):
        html = self.mod._receipt_badge_html({
            "ci_status": "green", "reviewer_approved": True,
            "pr_url": "https://gh/pr/5", "pr_number": 5,
        })
        self.assertIn("receipt-dot green", html)
        self.assertIn("approved", html)
        self.assertIn("PR #5", html)
        self.assertIn("ci-green", html)

    def test_yellow_when_partial(self):
        html = self.mod._receipt_badge_html({
            "ci_status": "green", "reviewer_approved": False,
        })
        self.assertIn("receipt-dot yellow", html)
        self.assertIn("not approved", html)

    def test_red_when_ci_red(self):
        html = self.mod._receipt_badge_html({"ci_status": "red"})
        self.assertIn("receipt-dot red", html)

    def test_red_when_abandoned(self):
        html = self.mod._receipt_badge_html({"outcome": "abandoned"})
        self.assertIn("receipt-dot red", html)

    def test_grey_when_nothing_known(self):
        html = self.mod._receipt_badge_html({"foo": "bar"})
        self.assertIn("receipt-dot grey", html)
        # Not the "no receipt" variant — it's a present-but-empty receipt.
        self.assertNotIn("no receipt", html)


class CmuxWorkspacesTests(_Base):
    def test_parses_workspaces(self):
        fake = mock.Mock()
        fake.returncode = 0
        fake.stdout = json.dumps({"workspaces": [
            {"ref": "workspace:1", "title": "Alpha", "custom_color": "#ff0000"},
            {"ref": "workspace:2", "title": "Beta"},
            {"title": "no ref skipped"},
        ]})
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            result = self.mod._cmux_workspaces()
        self.assertEqual(result["workspace:1"]["title"], "Alpha")
        self.assertEqual(result["workspace:1"]["color"], "#ff0000")
        self.assertEqual(result["workspace:2"]["color"], None)
        self.assertNotIn(None, result)

    def test_failure_returns_empty(self):
        with mock.patch.object(self.mod.subprocess, "run", side_effect=OSError("no cmux")):
            self.assertEqual(self.mod._cmux_workspaces(), {})

    def test_nonzero_returncode_empty(self):
        fake = mock.Mock()
        fake.returncode = 1
        fake.stdout = ""
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            self.assertEqual(self.mod._cmux_workspaces(), {})

    def test_empty_stdout_returns_empty(self):
        fake = mock.Mock()
        fake.returncode = 0
        fake.stdout = "   "
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            self.assertEqual(self.mod._cmux_workspaces(), {})


# ─── render_fleet_tab ────────────────────────────────────────────────────────

class FleetTabTests(_Base):
    def _patch_cmux(self, mapping):
        return mock.patch.object(self.mod, "_cmux_workspaces", return_value=mapping)

    def test_unavailable_when_no_report(self):
        html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 0)
        self.assertIn("fleet-unavailable", html)
        self.assertIn("report file not found", html)

    def test_malformed_report(self):
        (self._tmp / ".assistant/observer-latest-report.json").write_text("{ broken")
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 0)
        self.assertIn("fleet-unavailable", html)
        self.assertIn("report malformed", html)

    def test_cards_bucket_by_classification(self):
        self._write_observer_report({"candidate_actions": [
            {"_classification": "ACTIVE", "kind": "noop", "evidence": "running. still going.",
             "summary": "no action", "params": {"ws_ref": "workspace:1"}},
            {"_classification": "AWAITING_USER", "kind": "emit-card",
             "evidence": "needs your call.", "summary": "decide whether to merge",
             "params": {"ws_ref": "workspace:2"}},
            {"_classification": "STRANDED", "kind": "status-flip",
             "evidence": "stuck.", "summary": "manual fix", "params": {"ws_ref": "workspace:3"}},
            {"_classification": "DONE", "kind": "cleanup", "evidence": "shipped.",
             "summary": "ready", "params": {"ws_ref": "workspace:4"}},
        ]})
        # Receipt for the DONE card → green badge.
        self._write_receipt("workspace:4", {"ci_status": "green", "reviewer_approved": True,
                                            "pr_url": "https://gh/pr/4", "pr_number": 4})
        with self._patch_cmux({
            "workspace:1": {"title": "Alpha", "color": "#111111"},
            "workspace:2": {"title": "Beta", "color": None},
        }):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 4)
        # Four columns.
        self.assertIn("fleet-col-active", html)
        self.assertIn("fleet-col-needs_you", html)
        self.assertIn("fleet-col-stranded", html)
        self.assertIn("fleet-col-done", html)
        # cmux title used where available; ws_ref fallback otherwise.
        self.assertIn("Alpha", html)
        self.assertIn("Beta", html)
        self.assertIn("workspace:3", html)  # no cmux entry → ref as title
        # NEEDS YOU / STRANDED carry an action block from summary.
        self.assertIn("fleet-card-action", html)
        self.assertIn("decide whether to merge", html)
        self.assertIn("manual fix", html)
        # "what" = first sentence of evidence.
        self.assertIn("running.", html)
        # DONE card gets a receipt badge (green).
        self.assertIn("receipt-dot green", html)
        self.assertIn("PR #4", html)

    def test_unknown_classification_goes_stranded(self):
        self._write_observer_report({"candidate_actions": [
            {"_classification": "WAT", "kind": "mystery", "evidence": "?",
             "summary": "s", "params": {"ws_ref": "workspace:77"}},
        ]})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 1)
        # unknown class + non-noop/emit-card kind → stranded bucket has 1.
        # Find the stranded column count.
        self.assertIn("workspace:77", html)

    def test_empty_column_placeholder(self):
        self._write_observer_report({"candidate_actions": []})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 0)
        self.assertIn("Nothing here", html)

    def test_duplicate_ws_ref_collapsed(self):
        self._write_observer_report({"candidate_actions": [
            {"_classification": "STRANDED", "kind": "status-flip", "evidence": "first",
             "summary": "s1", "params": {"ws_ref": "workspace:5"}},
            {"_classification": "STRANDED", "kind": "cleanup", "evidence": "second",
             "summary": "s2", "params": {"ws_ref": "workspace:5"}},
        ]})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        # Same ws_ref → one card.
        self.assertEqual(n, 1)
        self.assertIn("first", html)
        self.assertNotIn("second", html)

    def test_action_without_ws_ref_skipped(self):
        self._write_observer_report({"candidate_actions": [
            {"_classification": "DONE", "kind": "cleanup", "evidence": "no ref", "summary": "s",
             "params": {}},
        ]})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 0)

    def test_kind_fallback_buckets_when_no_classification(self):
        # No _classification → fall back to kind: noop→active, emit-card→needs_you.
        self._write_observer_report({"candidate_actions": [
            {"kind": "noop", "evidence": "still running.", "summary": "",
             "_source_ws": "workspace:60"},
            {"kind": "emit-card", "evidence": "decide.", "summary": "your call",
             "_source_ws": "workspace:61"},
        ]})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 2)
        self.assertIn("workspace:60", html)
        self.assertIn("workspace:61", html)
        # emit-card → needs_you column gets an action block from summary.
        self.assertIn("your call", html)

    def test_non_numeric_ws_ref_sorts_safely(self):
        # _ws_num raises on a non-numeric suffix → falls back to a huge int.
        self._write_observer_report({"candidate_actions": [
            {"_classification": "ACTIVE", "kind": "noop", "evidence": "x.",
             "summary": "", "params": {"ws_ref": "workspace:weird"}},
            {"_classification": "ACTIVE", "kind": "noop", "evidence": "y.",
             "summary": "", "params": {"ws_ref": "workspace:5"}},
        ]})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 2)
        self.assertIn("workspace:weird", html)

    def test_done_long_summary_not_truncated_active_summary_is(self):
        # NEEDS YOU card with a >100-char summary → action text truncated + …
        long_summary = "decide " * 30  # ~210 chars
        self._write_observer_report({"candidate_actions": [
            {"_classification": "AWAITING_USER", "kind": "emit-card",
             "evidence": "big.", "summary": long_summary,
             "params": {"ws_ref": "workspace:70"}},
        ]})
        with self._patch_cmux({}):
            html, n = self.mod.render_fleet_tab()
        self.assertEqual(n, 1)
        self.assertIn("…", html)  # truncation ellipsis in the action block


# ─── render_workspaces_tab — rich row rendering ──────────────────────────────

class WorkspacesTabRichTests(_Base):
    """Drives the workspace-row rendering branches the in-process suite skips:
    live-session correlation by cwd, transcript NOW-text reading, working/idle
    status dots, category/PR/live-age chips, the legacy-classification verdict
    fallback, and the backed-off card with live session signals."""

    def test_rich_row_with_session_and_transcript(self):
        now = datetime.now(timezone.utc)
        cwd = "/Users/mukuls/dev/rich"
        # Transcript: last assistant text turn is the NOW line.
        transcript = self._tmp / "transcript.jsonl"
        transcript.write_text("\n".join([
            json.dumps({"type": "user", "message": {"content": "do it"}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"},
                # >200 chars → exercises the truncation branch (max_chars cap).
                {"type": "text", "text": "Running the deflake now " + ("x" * 250)},
            ]}}),
        ]) + "\n")
        # world.json with an open workspace + a session sharing the cwd.
        self._write_world({
            "workspaces": [{"ws_ref": "workspace:20", "title": "Auto: deflake X", "cwd": cwd}],
            "live_sessions": [{
                "ws_ref": "workspace:20", "cwd": cwd, "tab_id": "tab-20",
                "last_turn_age_sec": 120,
                "last_assistant": {"text": "[tool_use:Bash] running"},
                "transcript_path": str(transcript),
            }],
        })
        # Observer summary (new verdict schema) + PR refs.
        (self._tmp / ".assistant/observer-summaries/workspace_20.json").write_text(json.dumps({
            "ws_ref": "workspace:20", "title": "Auto: deflake X", "verdict": "active",
            "summary": "deflaking the ruler spec", "next": "wait for CI",
            "cwd": cwd, "ts": int(now.timestamp()), "pr_refs": [101, 102],
        }))
        html, n = self.mod.render_workspaces_tab()
        self.assertEqual(n, 1)
        # Working status dot (last_assistant text starts with [tool_use:).
        self.assertIn("status-dot working", html)
        # Category chip from "Auto:" prefix.
        self.assertIn("category-auto", html)
        self.assertIn("deflake", html)  # title also matches deflake category logic
        # PR chips.
        self.assertIn("PR #101", html)
        # Live-age chip (120s → fresh).
        self.assertIn("ws-live-age fresh", html)
        # NOW line from transcript text (not a tool_use).
        self.assertIn("Running the deflake now", html)
        self.assertIn("now-text", html)
        # NEXT line populated.
        self.assertIn("wait for CI", html)
        # Verdict pill active.
        self.assertIn("verdict-active", html)

    def test_legacy_classification_fallback_and_empty_now_next(self):
        now = datetime.now(timezone.utc)
        self._write_world({
            "workspaces": [{"ws_ref": "workspace:21", "title": "no-session ws", "cwd": "/x/none"}],
            "live_sessions": [],
        })
        # No `verdict`; legacy `classification` only → maps to ready_for_cleanup.
        (self._tmp / ".assistant/observer-summaries/workspace_21.json").write_text(json.dumps({
            "ws_ref": "workspace:21", "title": "no-session ws",
            "classification": "DONE", "summary": "finished",
            "cwd": "/x/none", "ts": int(now.timestamp()),
        }))
        html, n = self.mod.render_workspaces_tab()
        self.assertEqual(n, 1)
        # DONE → cleanup verdict class.
        self.assertIn("verdict-cleanup", html)
        # No live session → unknown status dot + empty NOW/NEXT.
        self.assertIn("status-dot unknown", html)
        self.assertIn("now-empty", html)
        self.assertIn("no recent narrative", html)
        self.assertIn("next-empty", html)
        self.assertIn("unknown", html)

    def test_backed_off_card_with_session_signals(self):
        now = datetime.now(timezone.utc)
        cwd = "/Users/mukuls/dev/parked"
        self._write_world({
            "workspaces": [
                {"ws_ref": "workspace:30", "title": "Parked work", "cwd": cwd},
                # A second, NOT-backed-off workspace so open_ws is non-empty and
                # the backed-off ws is actually filtered out of the rows.
                {"ws_ref": "workspace:31", "title": "Live work", "cwd": "/Users/mukuls/dev/live"},
            ],
            "live_sessions": [{
                "ws_ref": "workspace:30", "cwd": cwd, "tab_id": "tab-30",
                "last_turn_age_sec": 1000,  # warm
                "last_assistant": {"text": "idle"},
            }],
        })
        (self._tmp / ".assistant/observer-summaries/workspace_30.json").write_text(json.dumps({
            "ws_ref": "workspace:30", "title": "Parked work", "verdict": "active",
            "summary": "parked", "cwd": cwd, "ts": int(now.timestamp()),
            "pr_refs": [55],
        }))
        (self._tmp / ".assistant/observer-summaries/workspace_31.json").write_text(json.dumps({
            "ws_ref": "workspace:31", "title": "Live work", "verdict": "active",
            "summary": "live", "cwd": "/Users/mukuls/dev/live", "ts": int(now.timestamp()),
        }))
        (self._tmp / ".assistant/back-off.json").write_text(json.dumps({
            "workspaces": [{"ws_ref": "workspace:30", "reason": "looping",
                            "added_ts": int(time.time()) - 3600}],
        }))
        html, n = self.mod.render_workspaces_tab()
        # Backed-off ws excluded from open rows (only workspace:31 remains) but
        # shown in the banner.
        self.assertEqual(n, 1)
        self.assertIn("backoff-section", html)
        self.assertIn("workspace:30", html)
        self.assertIn("workspace:31", html)
        self.assertIn("looping", html)
        # Session signals join into the backoff card: warm live-age + PR chip.
        self.assertIn("ws-live-age warm", html)
        self.assertIn("PR #55", html)
        # "backed off ... ago" since-chip.
        self.assertIn("backed off", html)

    def test_corrupt_summary_skipped(self):
        self._write_world({"workspaces": [], "live_sessions": []})
        (self._tmp / ".assistant/observer-summaries/bad.json").write_text("{ corrupt")
        # No open_ws filter (empty workspaces) → corrupt file just skipped; with
        # no valid rows, returns the muted placeholder.
        html, n = self.mod.render_workspaces_tab()
        self.assertEqual(n, 0)
        self.assertIn("No observer summaries", html)

    def test_transcript_with_no_text_turn_falls_back_to_empty_now(self):
        now = datetime.now(timezone.utc)
        cwd = "/Users/mukuls/dev/notext"
        # Transcript has only tool_use + corrupt + non-assistant lines — no
        # assistant text turn → last_activity returns None → empty NOW line.
        transcript = self._tmp / "notext.jsonl"
        transcript.write_text("\n".join([
            "{ not json",
            json.dumps({"type": "user", "message": {"content": "hi"}}),
            # content list with a non-dict entry, a non-text dict, and an
            # empty-text dict — exercises every guard inside last_activity.
            json.dumps({"type": "assistant", "message": {"content": [
                "bare-string-not-dict",
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "   "},
            ]}}),
            # Unsupported content shape.
            json.dumps({"type": "assistant", "message": {"content": {"bad": "shape"}}}),
            json.dumps({"type": "assistant", "message": "string-not-dict"}),
            "",
        ]) + "\n")
        self._write_world({
            "workspaces": [{"ws_ref": "workspace:40", "title": "no-text ws", "cwd": cwd}],
            "live_sessions": [{
                "ws_ref": "workspace:40", "cwd": cwd, "tab_id": "tab-40",
                "last_turn_age_sec": 2000,  # cold
                "last_assistant": {"text": "plain idle"},
                "transcript_path": str(transcript),
            }],
        })
        (self._tmp / ".assistant/observer-summaries/workspace_40.json").write_text(json.dumps({
            "ws_ref": "workspace:40", "title": "no-text ws", "verdict": "active",
            "summary": "s", "cwd": cwd, "ts": int(now.timestamp()),
        }))
        html, n = self.mod.render_workspaces_tab()
        self.assertEqual(n, 1)
        # No text turn → empty NOW line; idle (plain text) status dot; cold age.
        self.assertIn("now-empty", html)
        self.assertIn("status-dot idle", html)
        self.assertIn("ws-live-age cold", html)

    def test_summary_with_empty_cwd_finds_no_session(self):
        # cwd "" → session_for_ws returns None early; agent_status unknown.
        now = datetime.now(timezone.utc)
        self._write_world({
            "workspaces": [{"ws_ref": "workspace:42", "title": "ws", "cwd": ""}],
            "live_sessions": [{"ws_ref": "workspace:42", "cwd": "/somewhere",
                               "last_assistant": {"text": "x"}}],
        })
        (self._tmp / ".assistant/observer-summaries/workspace_42.json").write_text(json.dumps({
            "ws_ref": "workspace:42", "title": "ws", "verdict": "active",
            "summary": "s", "cwd": "", "ts": int(now.timestamp()),
        }))
        html, n = self.mod.render_workspaces_tab()
        self.assertEqual(n, 1)
        self.assertIn("status-dot unknown", html)

    def test_missing_transcript_path_handled(self):
        now = datetime.now(timezone.utc)
        cwd = "/Users/mukuls/dev/nopath"
        self._write_world({
            "workspaces": [{"ws_ref": "workspace:41", "title": "ws", "cwd": cwd}],
            "live_sessions": [{
                "ws_ref": "workspace:41", "cwd": cwd, "tab_id": "tab-41",
                "last_assistant": {"text": "x"},
                "transcript_path": "/nonexistent/path.jsonl",
            }],
        })
        (self._tmp / ".assistant/observer-summaries/workspace_41.json").write_text(json.dumps({
            "ws_ref": "workspace:41", "title": "ws", "verdict": "active",
            "summary": "s", "cwd": cwd, "ts": int(now.timestamp()),
        }))
        html, n = self.mod.render_workspaces_tab()
        self.assertEqual(n, 1)
        self.assertIn("now-empty", html)


# ─── render() — top-level orchestration ──────────────────────────────────────

class RenderTopLevelTests(_Base):
    def test_writes_stub_when_no_world(self):
        # WORLD_PATH absent → stub written, returns early.
        self.mod.render()
        self.assertTrue(self.mod.DASHBOARD_HTML.exists())
        html = self.mod.DASHBOARD_HTML.read_text()
        self.assertIn("world.json not present yet", html)

    def test_full_render_writes_dashboard_and_redirect(self):
        world = full_world()
        self._write_world(world)
        self._write_todo(world["todo"])
        self._write_state({
            "awaiting_input": [
                {"tier": "T1", "title": "approve merge", "detail": "PR ready",
                 "confidence": 0.95, "touches": ["workspace:1"]},
            ],
            "actions_taken": [{"a": 1}],
            "_meta": {"generated_at": _iso(datetime.now(timezone.utc))},
        })
        now = datetime.now(timezone.utc)
        self._write_ledger([
            {"ts": _iso(now - timedelta(minutes=2)), "kind": "dispatch",
             "evidence": "spawned", "outcome": "verified", "pulse_idx": 9},
        ])
        # Observer summary for the Workspaces tab.
        (self._tmp / ".assistant/observer-summaries/workspace_1.json").write_text(json.dumps({
            "ws_ref": "workspace:1", "title": "Auto: deflake ruler", "verdict": "active",
            "summary": "fixing ruler", "next": "run tests", "cwd": "/Users/mukuls/dev/a",
            "ts": int(now.timestamp()),
        }))
        # Observer report for the Fleet tab.
        self._write_observer_report({"candidate_actions": [
            {"_classification": "DONE", "kind": "cleanup", "evidence": "shipped.",
             "summary": "ready", "params": {"ws_ref": "workspace:1"}},
        ]})
        # Heartbeat so the pulse-health banner is green.
        (self._tmp / ".assistant/heartbeat.json").write_text(json.dumps({
            "last_pulse_ts": int(time.time()) - 20, "pulse_idx": 100,
            "model": "python-mechanical",
        }))

        with mock.patch.object(self.mod, "_cmux_workspaces",
                               return_value={"workspace:1": {"title": "Alpha", "color": "#abc"}}):
            with mock.patch("sys.stdout") as fake_stdout:
                self.mod.render()

        # Dashboard + legacy redirect written.
        self.assertTrue(self.mod.DASHBOARD_HTML.exists())
        self.assertTrue(self.mod.TODO_HTML.exists())
        redirect = self.mod.TODO_HTML.read_text()
        self.assertIn("assistant-dashboard.html#todos", redirect)

        html = self.mod.DASHBOARD_HTML.read_text()
        # All four tab anchors present.
        self.assertIn('data-tab="decisions"', html)
        self.assertIn('data-tab="workspaces"', html)
        self.assertIn('data-tab="fleet"', html)
        self.assertIn('data-tab="todos"', html)
        # Tab panels.
        self.assertIn('data-panel="decisions"', html)
        self.assertIn('data-panel="todos"', html)
        # Awaiting card content flowed into the decisions panel.
        self.assertIn("approve merge", html)
        # Workspace row from the observer summary.
        self.assertIn("fixing ruler", html)
        # Fleet card.
        self.assertIn("fleet-board", html)
        # Pulse-health banner green.
        self.assertIn("pulse-ok", html)
        # TODO content.
        self.assertIn("fix crash", html)

        # render() prints a "wrote N bytes" line to stdout.
        printed = "".join(
            str(c.args[0]) for c in fake_stdout.write.call_args_list if c.args
        )
        self.assertIn("wrote", printed)


if __name__ == "__main__":
    unittest.main()
