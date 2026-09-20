"""Exercise rendering boundaries with saved local data, without live services."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    def attributes(self, tag):
        return [attrs for name, attrs in self.elements if name == tag]

    def content(self):
        return " ".join(self.text)


class RendererCoverageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix=".renderer-test-", dir=REPO)
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        # Exercise the standalone script's import bootstrap even in a test runner.
        paths = patch.object(sys, "path", [
            path for path in sys.path if path != str(REPO / "src")
        ])
        paths.start()
        self.addCleanup(paths.stop)
        spec = importlib.util.spec_from_file_location(
            "renderer_coverage", REPO / "bin/render-assistant-page.py")
        self.renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.renderer)
        self.assertIn(str(REPO / "src"), sys.path)
        clock = patch.object(self.renderer, "utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.world = {
            "_meta": {"built_at": NOW.isoformat()},
            "workspaces": [], "live_sessions": [], "todo": {"items": []},
        }

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def notifications(self, rows):
        path = self.write(".assistant/decisions/decisions.jsonl", {})
        records = [{
            "schema": "decision/1", "status": "open", "lane": "staged",
            "epoch": NOW.timestamp() - 60, **row,
        } for row in rows]
        path.write_text("\n \t\n" + "\n\n".join(map(json.dumps, records)) + "\n")
        return path

    def workspace(self, number=1, verdict="needs_user"):
        identity = {
            "workspace_id": f"workspace-{number}", "surface_id": f"surface-{number}",
            "provider": "claude", "session_id": f"session-{number}",
        }
        workspace = {
            "ws_ref": f"workspace:{number}", "title": f"Work {number}",
            "workspace_id": identity["workspace_id"],
            "surfaces": [{"surface_id": identity["surface_id"]}],
            "session_ids": [identity["session_id"]],
        }
        session = {
            **identity, "ws_ref": workspace["ws_ref"],
            "identity_status": "verified", "context_status": "verified",
            "context_built_at": NOW.isoformat(), "pending_tool_use": False,
            "guidance_context": {"source_version": "version-1"},
        }
        summary = {
            **identity, "ws_ref": workspace["ws_ref"], "verdict": verdict,
            "observed_sessions": [identity], "observation_complete": True,
            "ts": NOW.timestamp() - 60, "observed_at": NOW.timestamp() - 60,
            "summary": f"Current progress {number}.", "next": "Review the result.",
        }
        self.world["workspaces"].append(workspace)
        self.world["live_sessions"].append(session)
        self.write(f".assistant/observer-summaries/a-{number}.json", summary)
        return session, summary

    def note(self, **overrides):
        note = {
            "workspace_id": "workspace-1", "surface_id": "surface-1",
            "provider": "claude", "session_id": "session-1",
            "source_version": "version-1", "goal": "Preserve <goal>",
            "progress": "Saved <progress>", "recommendation": "continue",
            "who": "agent", "next_action": "Check <result> before continuing.",
            "rationale": "Evidence <not instructions>",
            "completion_evidence": [], "uncertainties": [],
            **overrides,
        }
        self.write(".assistant/session-return-notes.json", {"sessions": [note]})
        return note

    def overview(self):
        self.write(".claude/cache/world.json", self.world)
        markup, count = self.renderer.render_overview_tab(self.world)
        return Document(markup), count

    def test_live_queue_skips_blank_lines_and_preserves_recommendations(self):
        recommendation = {"class": "digest.append", "summary": "<keep this>", "args": {"x": 1}}
        path = self.notifications([
            {"id": "open", "title": "Keep <this>", "recommended": recommendation},
            {"id": "closed", "title": "Do not restore"},
            {"id": "closed", "status": "expired"},
        ])
        before = path.read_bytes()
        queue = self.renderer.brief_store.read_current_queue(NOW.timestamp())
        self.assertEqual([row["id"] for row in queue], ["open"])
        self.assertEqual(queue[0]["recommended"], recommendation)
        self.assertEqual(queue[0]["default_label"], "Accept: digest.append")
        markup, count = self.renderer.render_brief_tab()
        doc = Document(markup)
        self.assertEqual(count, 1)
        self.assertIn("Keep <this>", doc.content())
        self.assertIn("Accept: digest.append", doc.content())
        self.assertNotIn("Do not restore", doc.content())
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_goal_rows_do_not_change_valid_goal_ranking(self):
        records = [
            {"id": "ordinary", "status": "open", "epoch": NOW.timestamp(), "lane": "staged"},
            {"id": "goal", "status": "open", "epoch": NOW.timestamp(),
             "lane": "staged", "goal_refs": ["goal-1"]},
        ]
        valid = {"id": "goal-1", "rank": 1}
        queue = self.renderer.brief_store.build_queue(
            records, NOW.timestamp(), goals={"goals": [
                None, [], {"id": 9, "rank": 1}, {"id": "bad", "rank": "1"}, valid,
            ]})
        expected = self.renderer.brief_store.build_queue(
            records, NOW.timestamp(), goals={"goals": [valid]})
        self.assertEqual(queue, expected)
        self.assertEqual(queue[0]["id"], "goal")
        self.assertGreater(queue[0]["goal_boost"], queue[1]["goal_boost"])

    def test_dot_repository_names_never_group_notifications(self):
        self.notifications([
            {"id": f"{repo}-{number}", "source": "github", "title": f"{repo} alert {number}",
             "refs": {"repo": repo, "pr": 1}}
            for repo in ("owner/.", "owner/..") for number in (1, 2)
        ])
        markup, count = self.renderer.render_brief_tab()
        self.assertEqual(count, 4)
        self.assertIn("4 other notification groups", Document(markup).content())
        self.assertNotIn("GitHub pull requests", Document(markup).content())
        self.assertEqual(sum("data-dec-row" in attrs for _, attrs in Document(markup).elements), 4)

    def test_invalid_focus_objects_and_urls_preserve_current_alerts(self):
        self.notifications([{
            "id": "open", "source": "github", "title": "Original recommendation",
            "refs": {"repo": "owner/repo", "pr": 1},
            "recommended": {"class": "digest.append"},
        }])
        entry = {
            "repo": "owner/repo", "pr": 1, "headline": "Invalid replacement",
            "recommendation": "Do not apply this draft",
            "evidence_url": "https://github.com/owner/repo/pull/1",
            "checked_at": NOW.isoformat(),
        }
        invalid = [
            None, [], {}, {**entry, "headline": " "}, {**entry, "recommendation": 1},
            *[{**entry, "evidence_url": url} for url in (
                "https://[invalid", "https:///missing-host", "//github.com/owner/repo",
                "https://user:password@github.com/owner/repo/pull/1",
                "https://github.com/owner/repo/pull/1 bad", "javascript:alert(1)",
            )],
            {**entry, "checked_at": "2026-09-20T12:00:00"},
            {**entry, "checked_at": "not-a-date"},
        ]
        for focus in invalid:
            with self.subTest(focus=focus):
                self.write(".assistant/decisions/focus.json", {"topics": [focus]})
                markup, count = self.renderer.render_brief_tab()
                doc = Document(markup)
                self.assertEqual(count, 1)
                self.assertIn("Invalid focus entries ignored", doc.content())
                self.assertIn("Original recommendation", doc.content())
                self.assertIn("Accept: digest.append", doc.content())
                self.assertNotIn("Do not apply this draft", doc.content())

    def test_nonobject_brief_keeps_the_live_queue(self):
        self.notifications([{"id": "live", "title": "Still available"}])
        for value in ([], None, 7):
            with self.subTest(value=value):
                self.write(".assistant/brief/brief-2026-09-20.json", value)
                markup, count = self.renderer.render_brief_tab()
                self.assertEqual(count, 1)
                self.assertIn("Still available", markup)
                self.assertIn("couldn't be read", Document(markup).content())

    def test_bad_saved_objects_surface_errors_without_losing_live_cards(self):
        self.workspace()
        self.write(".assistant/observer-summaries/bad.json", [])
        self.write(".assistant/back-off.json", [])
        self.write(".assistant/session-return-notes.json", [])
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertIn("bad.json is not an object", doc.content())
        self.assertIn("back-off.json is not an object", doc.content())
        self.assertIn("saved session notes have an unexpected format", doc.content())
        self.assertIn("Current progress 1.", doc.content())

    def test_unreadable_notes_are_visible_and_missing_notes_are_optional(self):
        self.workspace()
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertNotIn("saved session notes couldn't be read", doc.content())
        path = self.home / ".assistant/session-return-notes.json"
        path.mkdir()
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertIn("saved session notes couldn't be read", doc.content())
        self.assertIn("Current progress 1.", doc.content())

    def test_duplicates_and_closed_summaries_do_not_create_cards(self):
        _, summary = self.workspace()
        self.world["workspaces"].extend([dict(self.world["workspaces"][0]), {}])
        self.write(".assistant/observer-summaries/z-older.json", {
            **summary, "ts": summary["ts"] - 60, "summary": "Obsolete progress",
        })
        self.write(".assistant/observer-summaries/z-closed.json", {
            **summary, "ws_ref": "workspace:closed", "summary": "Closed progress",
        })
        self.write(".assistant/observer-summaries/z-unidentified.json", {
            **summary, "ws_ref": "", "summary": "Unidentified progress",
        })
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertEqual([attrs["data-workspace-ref"] for attrs in doc.attributes("article")],
                         ["workspace:1"])
        self.assertIn("Current progress 1.", doc.content())
        for text in ("Obsolete progress", "Closed progress", "Unidentified progress"):
            self.assertNotIn(text, doc.content())

    def test_question_choices_escape_markup_and_never_send_answers(self):
        session, _ = self.workspace()
        session["guidance_context"]["pending_questions"] = [{
            "question": "Use <script> or wait?",
            "options": [
                {"label": "<button>Run</button>", "description": "<img src=x>"},
                {"label": "Wait & review"},
            ],
        }, {"question": "Any <other> requirement?", "options": []}]
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertIn("2 choices from your session", doc.content())
        self.assertIn("Use <script> or wait?", doc.content())
        self.assertIn("<button>Run</button>", doc.content())
        self.assertIn("<img src=x>", doc.content())
        self.assertIn("Any <other> requirement?", doc.content())
        self.assertIn("This page doesn't send a reply", doc.content())
        self.assertFalse(doc.attributes("script"))
        self.assertFalse(doc.attributes("img"))
        self.assertFalse(any("Run" == attrs.get("value") for attrs in doc.attributes("button")))

    def test_agent_note_preserves_evidence_uncertainty_and_readonly_prompt(self):
        self.workspace()
        note = self.note(
            completion_evidence=[
                {"url": "https://github.com/owner/repo/pull/42", "state": "OPEN"},
                {"path": "saved/<artifact>.md"},
                {"branch": "feature/<branch>", "registered": True},
                {"branch": "feature/<unregistered>", "registered": False},
                {"url": "javascript:alert(1)"},
                {"url": "https://example.com/unrelated"},
                {},
            ],
            uncertainties=["No <external> checks.", "Review & confirm."],
        )
        before = (self.home / ".assistant/session-return-notes.json").read_bytes()
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertIn("Pull request #42: open", doc.content())
        self.assertIn("saved/<artifact>.md", doc.content())
        self.assertIn("feature/<branch>", doc.content())
        self.assertIn("separate working copy exists", doc.content())
        self.assertIn("no separate working copy listed", doc.content())
        self.assertIn("No <external> checks. Review & confirm.", doc.content())
        self.assertIn(f"Goal: {note['goal']}", doc.content())
        self.assertIn(f"Next step: {note['next_action']}", doc.content())
        self.assertIn("Don't merge, delete, close sessions", doc.content())
        self.assertTrue(all("readonly" in attrs for attrs in doc.attributes("textarea")))
        self.assertEqual(len(doc.attributes("textarea")), 1)
        self.assertNotIn("javascript:alert(1)", [a.get("href") for a in doc.attributes("a")])
        self.assertFalse(doc.attributes("artifact"))
        self.assertEqual(before, (self.home / ".assistant/session-return-notes.json").read_bytes())

    def test_malformed_note_evidence_is_rejected_before_rendering(self):
        self.workspace()
        for evidence in ([None], [{"url": "https://[bad"}], [{"path": 7}]):
            with self.subTest(evidence=evidence):
                self.note(completion_evidence=evidence)
                doc, count = self.overview()
                self.assertEqual(count, 1)
                self.assertIn("1 saved notes couldn't be used", doc.content())
                self.assertNotIn("Preserve <goal>", doc.content())
                self.assertFalse(doc.attributes("textarea"))

    def test_unknown_note_stays_unknown_instead_of_proposing_closure(self):
        self.workspace()
        self.note(who="unknown", recommendation="unknown", next_action=None)
        doc, count = self.overview()
        self.assertEqual(count, 1)
        self.assertEqual(doc.attributes("article")[0]["data-lane"], "unknown")
        self.assertIn("Next step unclear", doc.content())
        self.assertIn("No other checks are recorded", doc.content())
        self.assertFalse(doc.attributes("textarea"))
        self.assertNotIn("You may be able to finish this task", doc.content())

    def test_invalid_dates_are_reported_without_hiding_old_tasks(self):
        self.write(".claude/assistant-todo.json", {"items": [
            {"id": "bad", "title": "Invalid date", "createdAt": "2026-02-30"},
            {"id": "today", "title": "Today's task", "createdAt": NOW.isoformat()},
            {"id": "tomorrow", "title": "Future task",
             "createdAt": (NOW + timedelta(days=1)).isoformat()},
            {"id": "old", "title": "Older <task>",
             "createdAt": (NOW - timedelta(days=2)).isoformat()},
        ]})
        doc, _ = self.overview()
        self.assertIn("Older <task>", doc.content())
        self.assertIn("1 task dates need checking", doc.content())
        self.assertIn("Finish one older task before adding another", doc.content())
        self.assertIn("old", [a.get("data-task-id") for a in doc.attributes("button")])
        self.assertNotIn("Invalid date", doc.content())
        self.assertNotIn("Future task", doc.content())

    def test_unreadable_task_dates_do_not_borrow_old_world_data(self):
        self.world["todo"]["items"] = [{
            "id": "stale", "title": "Stale world task", "createdAt": "2026-01-01",
        }]
        path = self.write(".claude/assistant-todo.json", {})
        path.write_text("{broken")
        doc, count = self.overview()
        self.assertEqual(count, 0)
        self.assertIn("Pending-task dates could not be loaded", doc.content())
        self.assertNotIn("Stale world task", doc.content())


if __name__ == "__main__":
    unittest.main()
