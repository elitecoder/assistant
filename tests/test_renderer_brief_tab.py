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
from unittest.mock import patch

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

    def write_log(self, rows):
        path = self.home / ".assistant/decisions/decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps({
            "schema": "decision/1", "status": "open", "epoch": NOW,
            "lane": "staged", **row,
        }) + "\n" for row in rows))
        return path

    def test_current_log_replaces_stale_brief_read_only(self):
        self.write_brief(brief_fixture())
        path = self.write_log([
            {"id": "current", "source": "github", "title": "Current alert",
             "refs": {"repo": "adobe/firefly-platform", "pr": 15561}},
            {"id": "closed", "title": "Closed alert"},
            {"id": "closed", "status": "expired", "epoch": NOW + 1},
        ])
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 1)
        self.assertIn("Current alert", html)
        self.assertNotIn("Closed alert", html)
        self.assertNotIn("dec-aaaa1111bbbb2222", html)
        self.assertIn("1 GitHub pull requests", html)
        self.assertIn("1 raw alerts", html)
        self.assertEqual(before, {
            p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        path.write_text(path.read_text() + json.dumps({
            "schema": "decision/1", "id": "current", "status": "expired",
            "epoch": NOW + 2,
        }) + "\n")
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 0)
        self.assertNotIn("Current alert", html)

    def test_github_grouping_keeps_other_sources_distinct(self):
        self.write_brief(brief_fixture())
        self.write_log([
            {"id": str(i), "source": source, "title": "Same title",
             "refs": {"repo": repo, "pr": pr}}
            for i, (source, repo, pr) in enumerate([
                ("github", "adobe/firefly-platform", 15561),
                ("github", "adobe/firefly-platform", "15561"),
                ("github", "other/repo", 15561),
                ("cmux", "adobe/firefly-platform", 15561),
                ("cmux", "adobe/firefly-platform", 15561),
                ("github", "invalid/repo/extra", 15561),
                ("github", "adobe/firefly-platform", True),
            ])
        ])
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 6)
        self.assertEqual(html.count('class="review-topic"'), 6)
        self.assertEqual(html.count('data-dec-row='), 7)
        self.assertIn("7 raw alerts", html)
        self.assertIn("2 GitHub pull requests", html)
        self.assertIn("4 other notification groups", html)
        self.assertIn("3 more notification groups", html)

    def test_missing_or_corrupt_log_explicitly_labels_snapshot(self):
        self.write_brief(brief_fixture())
        for contents in (None, "{torn", '{"schema":"decision/1","id":"x"}\n', '[]\n'):
            with self.subTest(contents=contents):
                if contents is not None:
                    self.write_log([]).write_text(contents)
                html, n = self.mod.render_brief_tab()
                self.assertEqual(n, 2)
                self.assertIn("Current notifications unavailable", html)
                self.assertIn("dated snapshot from 2026-07-02", html)
                self.assertIn("open status is unverified", html)
                self.assertNotIn("Queue clear", html)

    def test_current_queue_without_readable_brief(self):
        self.write_log([{"id": "live", "title": "Live notification"}])
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 1)
        self.assertIn("Live notification", html)
        self.assertIn("No brief yet", html)
        self.assertNotIn("data-brief-date=", html)
        (self.home / ".assistant/brief/brief-2026-07-02.json").write_text("{bad")
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 1)
        self.assertIn("Live notification", html)
        self.assertIn("unreadable", html)
        self.assertNotIn("data-brief-date=", html)

    def test_focus_order_matching_escaping_and_invalid_entries(self):
        self.write_brief(brief_fixture())
        self.write_log([
            {"id": str(pr), "source": "github", "title": f"Original {pr}",
             "refs": {"repo": "adobe/firefly-platform", "pr": pr}}
            for pr in (15561, 15723, 15795, 999)
        ])
        entry = {
            "repo": "adobe/firefly-platform", "pr": 15723,
            "headline": '<script>alert("headline")</script>',
            "recommendation": "<b>Draft only</b>",
            "evidence_url": 'https://github.com/adobe/firefly-platform/pull/15723?a="b"&c=d',
            "checked_at": "2026-09-19T16:00:00Z",
        }
        path = self.home / ".assistant/decisions/focus.json"
        path.write_text(json.dumps({"topics": [
            entry, {**entry, "pr": 15561, "headline": "Second focus"},
            {**entry, "pr": 12345, "headline": "Closed topic"},
            {**entry, "pr": 15795, "evidence_url": "javascript:alert(1)"},
            {**entry, "pr": 999, "checked_at": "not a date"},
        ]}))
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 4)
        self.assertLess(html.index("&lt;script&gt;"), html.index("Second focus"))
        self.assertIn("Checked at 2026-09-19T16:00:00Z", html)
        self.assertIn("&lt;b&gt;Draft only&lt;/b&gt;", html)
        self.assertIn('a=&quot;b&quot;&amp;c=d', html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("Closed topic", html)
        self.assertEqual(html.count('data-dec-row='), 4)
        self.assertIn("Invalid focus entries ignored", html)
        for bad in ("{torn", "[]", '{"topics":null}'):
            path.write_text(bad)
            html, n = self.mod.render_brief_tab()
            self.assertEqual(n, 4)
            self.assertEqual(html.count('data-dec-row='), 4)
            self.assertIn("all current topics remain listed", html)

    def test_focus_never_promotes_unverified_snapshot(self):
        doc = brief_fixture()
        doc["queue"][1]["refs"] = {"repo": "adobe/firefly-platform", "pr": 15561}
        self.write_brief(doc)
        directory = self.home / ".assistant/decisions"
        directory.mkdir()
        (directory / "focus.json").write_text(json.dumps({"topics": [{
            "repo": "adobe/firefly-platform", "pr": 15561,
            "headline": "Unverified focus", "recommendation": "Do not promote",
            "evidence_url": "https://github.com/adobe/firefly-platform/pull/15561",
            "checked_at": "2026-09-19T16:00:00Z",
        }]}))
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 2)
        self.assertNotIn("Unverified focus", html)

    def test_old_narration_cannot_restore_decision_count(self):
        self.write_brief(brief_fixture())
        self.write_log([{"id": "new"}])
        with patch("assistant.narrator.narrative_for_brief", return_value={
                "summary": "2259 decisions pending", "source": "llm"}):
            html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 1)
        self.assertNotIn("2259", html)
        self.assertNotIn("Decisions pending", html)

    def test_live_recommendations_ignore_saved_narration_for_changed_records(self):
        doc = brief_fixture()
        row = dict(doc["queue"][1])
        row["snippet"] = "Old cached snippet"
        doc["queue"] = [row]
        self.write_brief(doc)
        (self.home / f".assistant/brief/brief-{doc['date']}.narrative.json").write_text(
            json.dumps({
                "schema": "brief-narrative/1", "date": doc["date"],
                "brief_epoch": doc["epoch"], "source": "llm",
                "summary": "Old summary",
                "recommendations": {row["id"]: "Obsolete saved recommendation"},
            }))
        self.write_log([row, {
            **row, "epoch": NOW + 1,
            "recommended": {"class": "digest.append"},
            "snippet": "Updated current evidence",
        }])
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 1)
        self.assertIn(f'data-dec-row="{row["id"]}"', html)
        self.assertIn("Updated current evidence", html)
        self.assertIn("Accept: digest.append", html)
        self.assertNotIn("Obsolete saved recommendation", html)
        self.assertNotIn("Old cached snippet", html)
        self.assertNotIn("Accept: todo.create", html)
        with patch("assistant.narrator.narrative_for_brief") as saved_narration:
            self.mod.render_brief_tab()
        saved_narration.assert_not_called()

    def test_focus_requires_timezone_aware_checked_at(self):
        self.write_brief(brief_fixture())
        self.write_log([{"id": "focus-alert", "title": "Original topic",
                         "source": "github", "refs": {"repo": "adobe/firefly-platform", "pr": 15561}}])
        path = self.home / ".assistant/decisions/focus.json"
        for stamp, valid in (("2026-09-19T16:00:00", False),
                             ("2026-09-19T16:00:00Z", True),
                             ("2026-09-19T09:00:00-07:00", True)):
            with self.subTest(stamp=stamp):
                path.write_text(json.dumps({"topics": [{
                    "repo": "adobe/firefly-platform", "pr": 15561,
                    "headline": "Curated topic", "recommendation": "Curated draft",
                    "evidence_url": "https://github.com/adobe/firefly-platform/pull/15561",
                    "checked_at": stamp,
                }]}))
                html, n = self.mod.render_brief_tab()
                self.assertEqual(n, 1)
                self.assertEqual("Curated topic" in html, valid)
                self.assertEqual("Curated draft" in html, valid)
                self.assertIn('data-dec-row="focus-alert"', html)
                if valid:
                    self.assertIn(f"Checked at {stamp}", html)
                else:
                    self.assertIn("Invalid focus entries ignored", html)

    def test_alert_created_after_focus_unpins_topic_without_hiding_history(self):
        self.write_brief(brief_fixture())
        checked_at = "2026-09-19T10:10:00-07:00"
        checked_epoch = datetime.fromisoformat(checked_at).timestamp()
        old = {"id": "old-alert", "title": "Old topic alert", "source": "github",
               "refs": {"repo": "adobe/firefly-platform", "pr": 15561},
               "created_epoch": NOW, "epoch": checked_epoch + 60}
        urgent = {"id": "urgent-alert", "title": "Urgent other topic", "source": "github",
                  "lane": "escalate", "refs": {"repo": "adobe/firefly-platform", "pr": 15723}}
        self.write_log([old, urgent])
        focus = self.home / ".assistant/decisions/focus.json"
        focus.write_text(json.dumps({"topics": [{
            "repo": "adobe/firefly-platform", "pr": 15561,
            "headline": "Prepared topic", "recommendation": "Previously checked draft",
            "evidence_url": "https://github.com/adobe/firefly-platform/pull/15561",
            "checked_at": checked_at,
        }]}))
        before = focus.read_bytes()
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 2)
        self.assertLess(html.index("<h3>Prepared topic"), html.index("<h3>Urgent other topic"))
        for seconds_after in (0, 1):
            with self.subTest(seconds_after=seconds_after):
                self.write_log([old, urgent, {
                    **old, "id": "new-alert", "title": "New topic alert",
                    "created_epoch": checked_epoch + seconds_after,
                }])
                html, n = self.mod.render_brief_tab()
                self.assertEqual(n, 2)
                self.assertEqual(html.count('data-dec-row='), 3)
                self.assertIn("3 raw alerts", html)
                for dec_id in ("old-alert", "urgent-alert", "new-alert"):
                    self.assertIn(f'data-dec="{dec_id}" data-action="accept"', html)
                self.assertEqual(focus.read_bytes(), before)
                if seconds_after == 0:
                    self.assertIn("Prepared topic", html)
                    self.assertIn("Previously checked draft", html)
                    self.assertNotIn("dated focus ignored", html)
                else:
                    self.assertNotIn("Prepared topic", html)
                    self.assertNotIn("Previously checked draft", html)
                    self.assertIn(f"adobe/firefly-platform #15561 changed after {checked_at}", html)
                    self.assertIn("dated focus ignored", html)
                    self.assertIn("Review new alerts and recheck the draft", html)
                    self.assertLess(html.index("<h3>Urgent other topic"),
                                    html.index("<h3>New topic alert"))

    def test_no_brief_yet_degrades_to_message(self):
        html, n = self.mod.render_brief_tab()
        self.assertEqual(n, 0)
        self.assertIn("No brief yet", html)
        self.assertIn("Notification counts unavailable", html)
        self.assertNotIn("0 GitHub pull requests", html)
        self.assertNotIn("data-brief-date=", html)

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

    def test_dark_provider_alarms_in_health(self):
        # A5: an opted-in droid that goes dark books failed cost rows every
        # pulse; the brief health chip must surface it LOUD (cold + FAILING) so
        # the fleet never reads green while its LLM driver is blind. A healthy
        # provider with zero failures stays quiet.
        doc = brief_fixture()
        doc["health"]["providers"] = {
            "droid": {"calls": 8, "failed": 8, "trailing_failures": 8,
                      "failing": True, "last_failed_caller": "triage"},
            "claude": {"calls": 12, "failed": 0, "trailing_failures": 0,
                       "failing": False, "last_failed_caller": None},
        }
        self.write_brief(doc)
        html, _ = self.mod.render_brief_tab()
        self.assertIn("droid · FAILING", html)       # loud
        self.assertIn("cold", html)                  # alarming class
        self.assertNotIn("claude · FAILING", html)   # healthy provider stays quiet

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

    def test_all_alert_controls_remain_reachable_in_history_chunks(self):
        for count in (45, 85):
            with self.subTest(count=count):
                doc = brief_fixture()
                row = {**doc["queue"][1],
                       "refs": {"repo": "adobe/firefly-platform", "pr": 15561}}
                doc["queue"] = [{**row, "id": f"dec-{i:016x}"} for i in range(count)]
                self.write_brief(doc)
                self.write_log(doc["queue"])
                html, n = self.mod.render_brief_tab()
                self.assertEqual(n, 1)
                self.assertEqual(html.count("data-dec-row="), count)
                self.assertEqual(html.count('class="notification-chunk"'), (count - 1) // 40)
                self.assertIn(f"{count} raw alerts", html)
                self.assertNotIn("not displayed", html)
                self.assertNotIn("history is capped", html)
                for item in doc["queue"]:
                    for action in ("accept", "snooze", "reject", "wrong_lane"):
                        self.assertIn(f'data-dec="{item["id"]}" data-action="{action}"', html)
                for start in range(40, count, 40):
                    self.assertIn(f"Alerts {start + 1}-{min(start + 40, count)} of {count}", html)

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
