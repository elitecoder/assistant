"""Tests for the Brief tab in bin/render-assistant-page.py (Keel M3).

Same harness as test_renderer_tabs.py: $HOME is pointed at a tmp dir BEFORE
the module is imported (its path constants bind at import), the tab function
is driven with real files, and we assert the DATA-DRIVEN branches — action
buttons, provenance, receipts, digest groups, health chips, the degradation
messages — never verbatim HTML. The load-bearing assertion set is M0's
lesson: any brief failure degrades to a message div; the page never breaks.
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/render-assistant-page.py"

NOW = datetime(2026, 7, 2, 10, 0).timestamp()


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("renderer_brief_mod",
                                                  str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def brief_fixture() -> dict:
    return {
        "schema": "morning-brief/1",
        "date": "2026-07-02",
        "ts": "2026-07-02T14:00:00Z",
        "epoch": int(NOW),
        "wake_hour": 7,
        "queue": [
            {"id": "dec-aaaa1111bbbb2222", "title": "workspace:7 needs_input",
             "source": "cmux", "kind": "needs_input", "lane": "escalate",
             "policy_id": "cmux-needs-input-escalate", "urgency": "now",
             "ttl_h": None, "created_ts": "2026-07-02T13:00:00Z",
             "age_h": 1.0, "score": 170.0, "recommended": None,
             "default_action": "accept", "default_label": "Accept",
             "triage": {"suggested_lane": "staged", "rationale": "looks routine"},
             "ws_ref": "workspace:7", "snippet": "approve tool use?"},
            {"id": "dec-cccc3333dddd4444", "title": "review PR #42",
             "source": "github", "kind": "review_requested", "lane": "staged",
             "policy_id": "gh-review-staged", "urgency": None, "ttl_h": 72,
             "created_ts": "2026-07-02T09:00:00Z", "age_h": 5.0,
             "score": 79.0,
             "recommended": {"class": "todo.create", "summary": "review PR"},
             "default_action": "accept",
             "default_label": "Accept: todo.create",
             "triage": None, "ws_ref": None, "snippet": ""},
        ],
        "handled_overnight": [
            {"ts": "2026-07-02T05:00:00Z", "kind": "decision-auto-done",
             "key": "decision:dec-x:auto_done", "ws_ref": "(events)",
             "outcome": "verified",
             "evidence": "policy rule-auto auto-handled cmux/work_complete"},
        ],
        "digest": {
            "cmux": [{"ts": "2026-07-02T04:00:00Z", "kind": "crash_event",
                      "title": "workspace:3 crash", "policy_id": "rule-digest"}],
        },
        "health": {
            "event_sources": {"cmux": {"count_24h": 9,
                                       "latest_ts": "2026-07-02T13:58:00Z",
                                       "latest_age_sec": 120}},
            "events_24h": 9,
            "quarantine_pending": 2,
            "world_built_at": "2026-07-02T13:59:00Z",
            "interrupts": {"delivered_24h": 0, "denied_24h": 7,
                           "budget": {"page": 0, "notify": 0}},
            "cost": {"cost_per_day_usd": 12.34,
                     "cost_ledger_per_day_usd": 1.0, "n_pulses_7d": 100},
            "expired_unseen_24h": 3,
            "connectors": {},
        },
        "goals": {"available": False, "note": "goals store absent until M4"},
        "counts": {"open_decisions": 2, "by_lane": {"escalate": 1, "staged": 1},
                   "handled_overnight": 1, "digest_rows": 1},
    }


class BriefTabTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        (self.home / ".assistant/brief").mkdir(parents=True)
        (self.home / ".claude/cache").mkdir(parents=True)
        self.mod = load_module(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def write_brief(self, doc):
        (self.home / ".assistant/brief" / f"brief-{doc['date']}.json"
         ).write_text(json.dumps(doc))

    def test_no_brief_yet_degrades_to_message(self):
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 0)
        self.assertIn("No brief yet", html)

    def test_corrupt_brief_degrades_to_message(self):
        (self.home / ".assistant/brief/brief-2026-07-02.json"
         ).write_text("{torn write")
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 0)
        self.assertIn("unreadable", html)
        self.assertIn("build-morning-brief.py", html)

    def test_unexpected_shape_never_raises(self):
        (self.home / ".assistant/brief/brief-2026-07-02.json"
         ).write_text(json.dumps({"queue": "not-a-list"}))
        html, n = self.mod.render_brief_tab()  # must not raise (M0's lesson)
        self.assertEqual(n, 0)
        self.assertIn("Brief unavailable", html)

    def test_f2_errored_but_polling_connector_alarms_in_health(self):
        # F2: a connector that errors every poll refreshes last_poll on each
        # failed poll, so it is never "stale" — but classify_connector marked it
        # error/ok:false. The brief health chip must surface it as a PROBLEM
        # (cold + reason), not render it "fresh" forever. A not_configured
        # connector stays quiet (no chip).
        doc = brief_fixture()
        doc["health"]["connectors"] = {
            "github": {"status": "error", "ok": False, "stale": False,
                       "token_expired": False, "errors": ["boom 500"],
                       "last_poll": "2026-07-02T13:59:00Z"},
            "gmail": {"status": "not_configured", "ok": False, "stale": False,
                      "token_expired": False, "errors": [], "last_poll": None},
        }
        self.write_brief(doc)
        html, _ = self.mod.render_brief_tab()
        self.assertEqual(html.count("connector heartbeat · github"), 1)  # one chip
        self.assertIn("github · error", html)                    # alarming
        self.assertIn("cold", html)                              # not "fresh"
        self.assertNotIn("github · 2026-07-02T13:59:00Z", html)  # NOT shown fresh
        self.assertNotIn("connector heartbeat · gmail", html)    # nc stays quiet

    def test_full_brief_renders_all_four_sections(self):
        self.write_brief(brief_fixture())
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 2)
        # Root carries the date for the /brief/seen ping.
        self.assertIn('data-brief-date="2026-07-02"', html)
        # 1. queue rows: id, provenance, one-tap buttons incl. wrong_lane.
        self.assertIn("dec-aaaa1111bbbb2222", html)
        self.assertIn("via cmux-needs-input-escalate", html)
        self.assertIn('data-action="accept"', html)
        self.assertIn('data-action="reject"', html)
        self.assertIn('data-action="snooze"', html)
        self.assertIn('data-action="wrong_lane"', html)
        # The recommended default action labels the button.
        self.assertIn("Accept: todo.create", html)
        # Triage suggestion is shown as an annotation.
        self.assertIn("triage suggests", html)
        # 2. receipts.
        self.assertIn("decision-auto-done", html)
        self.assertIn("rule-auto", html)
        # 3. digest grouped + collapsed.
        self.assertIn("digest-group", html)
        self.assertIn("<details", html)
        # 4. health: staleness chip, interrupts tile, $/day, expired-unseen.
        self.assertIn("cmux · 2m", html)
        self.assertIn("0 / 7", html)
        self.assertIn("$12.34", html)
        self.assertIn("quarantine · 2", html)
        self.assertIn("noise budget", html)

    def test_seen_state_reflected(self):
        doc = brief_fixture()
        self.write_brief(doc)
        html, _ = self.mod.render_brief_tab()
        self.assertIn("unseen", html)
        (self.home / ".assistant/brief/brief-2026-07-02.seen.json"
         ).write_text(json.dumps({"seen_ts": "2026-07-02T15:00:00Z"}))
        html2, _ = self.mod.render_brief_tab()
        self.assertNotIn("unseen —", html2)

    def test_trend_sparkline_from_metrics_rows(self):
        self.write_brief(brief_fixture())
        metrics = self.home / ".assistant/brief/brief-metrics.jsonl"
        rows = [{"date": f"2026-07-0{i}", "decisions_pending_at_brief": i}
                for i in range(1, 3)]
        metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        html, _ = self.mod.render_brief_tab()
        self.assertIn("brief-spark", html)
        # A single row is not a trend — no sparkline, no crash.
        metrics.write_text(json.dumps(rows[0]) + "\n")
        html2, _ = self.mod.render_brief_tab()
        self.assertNotIn("brief-spark", html2)

    def test_latest_brief_wins(self):
        old = brief_fixture()
        old["date"] = "2026-07-01"
        old["queue"] = []
        self.write_brief(old)
        self.write_brief(brief_fixture())
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 2)
        self.assertIn('data-brief-date="2026-07-02"', html)

    def test_whole_page_render_includes_brief_tab(self):
        """render() must ship the Brief tab (button + panel + seen ping)
        and still write the page when the brief store is empty."""
        (self.home / ".claude/cache/world.json").write_text(json.dumps(
            {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
        self.write_brief(brief_fixture())
        self.mod.render()
        page = (self.home / ".claude/assistant-dashboard.html").read_text()
        self.assertIn("showTab('brief')", page)
        self.assertIn('data-panel="brief"', page)
        self.assertIn("pingBriefSeen", page)
        self.assertIn("/brief/seen", page)
        self.assertIn("handleDecisionActClick", page)

    def test_queue_render_is_capped_with_more_row(self):
        """F18: the queue is capped with an 'N more' row like the receipts
        and digest sections — an incident-day queue can't render unbounded."""
        doc = brief_fixture()
        row = dict(doc["queue"][1])
        doc["queue"] = []
        for i in range(45):  # > the 40-row cap
            r = dict(row)
            r["id"] = f"dec-{i:016x}"
            doc["queue"].append(r)
        self.write_brief(doc)
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 45)                      # count is honest …
        self.assertEqual(html.count("data-dec-row="), 40)  # … render is capped
        self.assertIn("more decision", html)

    def test_seen_ping_uses_absolute_server_url(self):
        """F19: the /brief/seen ping targets the todo-server's absolute origin
        so it still reaches the server from a file:// dashboard (a relative
        fetch would resolve to file:///brief/seen and silently arm the
        destructive unseen-TTL)."""
        (self.home / ".claude/cache/world.json").write_text(json.dumps(
            {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
        self.write_brief(brief_fixture())
        self.mod.render()
        page = (self.home / ".claude/assistant-dashboard.html").read_text()
        self.assertIn("127.0.0.1:9876/brief/seen", page)
        # The bare relative form must be gone.
        self.assertNotIn("fetch('/brief/seen", page)


if __name__ == "__main__":
    unittest.main()
