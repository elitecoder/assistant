"""Exercise the attention overview against the renderer's real file inputs."""

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
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


class Cards(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = {}
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "article" and "attention-card" in attrs.get("class", ""):
            self.cards[attrs["data-workspace-ref"]] = attrs


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for directory in (".claude/cache", ".assistant/observer-summaries"):
            (self.home / directory).mkdir(parents=True)
        spec = importlib.util.spec_from_file_location(
            "overview_renderer", REPO / "bin/render-assistant-page.py")
        self.renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.renderer)
        clock = mock.patch.object(self.renderer, "utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.world = {
            "_meta": {"built_at": NOW.isoformat()},
            "workspaces": [], "live_sessions": [], "counts": {}, "todo": {},
        }

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def workspace(self, number, verdict="active", age=60, **overrides):
        ref = f"workspace:{number}"
        title = f"Task {number}"
        identity = {
            "surface_id": f"surface-id-{number}", "provider": "claude",
            "session_id": f"session-{number}",
        }
        self.world["workspaces"].append({
            "ws_ref": ref, "title": title, "workspace_id": f"workspace-id-{number}",
            "surfaces": [{"surface_id": identity["surface_id"]}],
            "session_ids": [identity["session_id"]],
        })
        self.world["live_sessions"].append({
            **identity, "ws_ref": ref, "workspace_id": f"workspace-id-{number}",
            "cwd": "/work/project", "identity_status": "verified",
            "context_status": "verified", "context_built_at": NOW.isoformat(),
            "pending_tool_use": False,
        })
        summary = {
            "ws_ref": ref, "title": title, "verdict": verdict,
            "workspace_id": f"workspace-id-{number}", "observed_sessions": [identity],
            "observation_complete": True,
            "ts": (NOW - timedelta(seconds=age)).timestamp(),
            "observed_at": (NOW - timedelta(seconds=age)).timestamp(),
            "summary": f"Recorded outcome for {number}.",
            "next": f"Review the next result for {number}.",
            "cwd": "/work/project",
        }
        summary.update(overrides)
        self.write(f".assistant/observer-summaries/workspace_{number}.json", summary)
        return ref

    def render(self):
        self.write(".claude/cache/world.json", self.world)
        return self.renderer.render_overview_tab(self.world)

    def test_groups_real_summary_and_pause_records(self):
        self.workspace(1, "needs_user")
        self.workspace(2, "active")
        self.workspace(3, "ready_for_merge")
        self.workspace(4, "ready_for_cleanup")
        parked = self.workspace(5, "active")
        self.write(".assistant/back-off.json", {
            "workspaces": [{"ws_ref": parked, "workspace_id": "workspace-id-5",
                            "reason": "Wait for the design decision."}]})
        html, count = self.render()
        cards = Cards(html).cards
        self.assertEqual(count, 5)
        for number, lane in [(1, "needs-you"), (2, "working"), (3, "ready"),
                             (4, "ready"), (5, "parked")]:
            self.assertIn(f"lane-{lane}", cards[f"workspace:{number}"]["class"])
        self.assertIn("Wait for the design decision.", html)
        self.assertNotIn("close-workspace", html)
        self.assertNotIn("onclick=\"merge", html)

    def test_missing_summary_is_visible_as_unknown(self):
        self.world["workspaces"] = [{"ws_ref": "workspace:1", "title": "New work"}]
        html, count = self.render()
        self.assertEqual(count, 1)
        self.assertIn("Status unknown", html)
        self.assertIn("No matching return note", html)

    def test_closed_workspace_summaries_never_reappear(self):
        self.workspace(1)
        self.world["workspaces"] = []
        html, count = self.render()
        self.assertEqual(count, 0)
        self.assertNotIn("Recorded outcome", html)

    def test_recycled_reference_with_same_title_does_not_borrow_context(self):
        self.workspace(1, workspace_id="previous-workspace", summary="Wrong task's private context.")
        html, _ = self.render()
        self.assertIn("Status unknown", html)
        self.assertNotIn("Wrong task", html)

    def test_old_and_future_snapshots_are_not_actionable(self):
        self.workspace(1, "ready_for_cleanup")
        for delta in (-601, 60):
            with self.subTest(delta=delta):
                self.world["_meta"]["built_at"] = (NOW + timedelta(seconds=delta)).isoformat()
                html, _ = self.render()
                self.assertIn("Status unknown", html)
                self.assertIn(" disabled", html)
                self.assertIn("Old data cannot tell you", html)

    def test_old_context_is_not_a_current_completion_claim(self):
        self.workspace(1, "ready_for_cleanup", age=601)
        html, _ = self.render()
        self.assertIn("Status unknown", html)
        self.assertNotIn("Check before closing", html)

    def test_failed_observation_is_not_evidence_of_work(self):
        self.workspace(1, "active", observation_complete=False)
        html, _ = self.render()
        self.assertIn("Status unknown", html)
        self.assertNotIn("In progress", html)

    def test_fresh_scan_does_not_make_old_session_context_current(self):
        self.workspace(1, "active", age=601)
        self.world["live_sessions"][0].update({
            "context_built_at": (NOW - timedelta(days=15)).isoformat(),
            "last_assistant": {"ts": NOW.isoformat(), "text": "[tool_use:Bash] old run"},
        })
        html, _ = self.render()
        self.assertIn("Status unknown", html)
        self.assertNotIn("Last signal: tool activity", html)

    def test_newer_tool_activity_prevents_close_nudge(self):
        ref = self.workspace(1, "ready_for_cleanup", age=120)
        self.world["live_sessions"][0].update({
            "pending_tool_use": True,
            "last_assistant": {
                "ts": (NOW - timedelta(seconds=10)).isoformat(),
                "text": "[tool_use:Bash] build",
            },
        })
        html, _ = self.render()
        self.assertIn("Last signal: tool activity", html)
        self.assertNotIn("One task may be ready", html)

    def test_mixed_text_and_pending_tool_does_not_trigger_wrap_up(self):
        self.workspace(1, "ready_for_cleanup")
        self.world["live_sessions"][0].update({
            "first_recorded_at": "2026-09-01T10:00:00Z",
            "pending_tool_use": True,
            "last_assistant": {
                "ts": NOW.isoformat(),
                "text": "I am checking the final result.\n[tool_use:Bash] run checks",
            },
        })
        html, _ = self.render()
        self.assertIn("lane-working", Cards(html).cards["workspace:1"]["class"])
        self.assertNotIn("Wrap up an older session", html)
        self.assertNotIn("Check before closing", html)

    def test_unknown_pending_tool_state_cannot_trigger_wrap_up(self):
        self.workspace(1, workspace_id="previous-workspace")
        self.world["live_sessions"][0].update({
            "first_recorded_at": "2026-09-01T10:00:00Z",
            "pending_tool_use": None,
            "last_assistant": {"ts": NOW.isoformat(), "text": "Partial tool context."},
        })
        html, _ = self.render()
        self.assertIn("Tool status unknown", html)
        self.assertNotIn("Wrap up an older session", html)

    def test_completion_summary_cannot_override_unknown_tools(self):
        self.workspace(1, "ready_for_cleanup")
        self.world["live_sessions"][0].update({
            "first_recorded_at": "2026-09-01T10:00:00Z", "pending_tool_use": None})
        html, _ = self.render()
        self.assertIn("Tool status unknown", html)
        self.assertNotIn("Check before closing", html)
        self.assertNotIn("Wrap up an older session", html)

    def test_every_associated_surface_needs_known_completed_tools(self):
        self.workspace(1)
        first = self.world["live_sessions"][0]
        first.update({
            "first_recorded_at": "2026-09-01T10:00:00Z",
            "last_assistant": {"ts": NOW.isoformat(), "text": "The first session finished."}})
        second = {**first, "session_id": "second-session", "surface_id": "second-surface",
                  "provider": "droid", "last_assistant": {
                      "ts": (NOW - timedelta(seconds=10)).isoformat(), "text": "Other session."}}
        self.world["live_sessions"].append(second)
        self.world["workspaces"][0]["session_ids"].append("second-session")
        self.world["workspaces"][0]["surfaces"].append({"surface_id": "second-surface"})
        for pending, status in ((None, "verified"), (False, "unknown")):
            with self.subTest(pending=pending, context=status):
                second.update({"pending_tool_use": pending, "context_status": status})
                html, _ = self.render()
                self.assertIn("Tool status unknown", html)
                self.assertNotIn("Wrap up an older session", html)
        second.update({"pending_tool_use": False, "context_status": "verified"})
        html, _ = self.render()
        self.assertNotIn("Wrap up an older session", html)
        self.assertIn("lane-updates", Cards(html).cards["workspace:1"]["class"])

    def test_real_transcript_tool_lifecycle_controls_the_finish_prompt(self):
        spec = importlib.util.spec_from_file_location(
            "overview_transcript_reader", REPO / "bin/session-context-watcher.py")
        watcher = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = watcher
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(watcher)
        transcript = self.home / "session-1.jsonl"
        events = [
            {"type": "user", "uuid": "user-1", "parentUuid": None,
             "timestamp": NOW.isoformat(), "message": {"role": "user", "content": "Run the checks."}},
            {"type": "assistant", "uuid": "assistant-1", "parentUuid": "user-1",
             "timestamp": NOW.isoformat(), "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "I am checking the result."},
                 {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "checks"}},
             ]}},
        ]
        transcript.write_text("".join(json.dumps(event) + "\n" for event in events))
        state = watcher.TranscriptState(transcript, "/work/project", provider="claude")
        state.read_new()
        context = state.to_dict(NOW)
        self.assertIs(context["pending_tool_use"], True)
        self.workspace(1, "ready_for_cleanup")
        self.world["live_sessions"][0].update(context)
        self.world["live_sessions"][0]["first_recorded_at"] = "2026-09-01T10:00:00Z"
        html, _ = self.render()
        self.assertIn("lane-working", Cards(html).cards["workspace:1"]["class"])
        self.assertNotIn("Wrap up an older session", html)
        completed = [
            {"type": "user", "uuid": "result-1", "parentUuid": "assistant-1",
             "timestamp": NOW.isoformat(), "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tool-1", "content": "Checks passed."},
             ]}},
            {"type": "assistant", "uuid": "assistant-2", "parentUuid": "result-1",
             "timestamp": NOW.isoformat(), "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "The recorded checks passed. Review the change."},
             ]}},
        ]
        with transcript.open("a") as stream:
            stream.write("".join(json.dumps(event) + "\n" for event in completed))
        state.read_new()
        context = state.to_dict(NOW)
        self.assertIs(context["pending_tool_use"], False)
        self.world["live_sessions"][0].update(context)
        html, _ = self.render()
        self.assertNotIn("Wrap up an older session", html)
        self.assertIn("lane-updates", Cards(html).cards["workspace:1"]["class"])
        self.assertIn("The recorded checks passed. Review the change.", html)

    def test_same_folder_does_not_link_another_workspace_session(self):
        self.workspace(1)
        self.world["live_sessions"].append({
            "ws_ref": "workspace:2", "cwd": "/work/project",
            "session_id": "wrong-session",
        })
        html, _ = self.render()
        self.assertNotIn("wrong-session", html)

    def test_session_replacement_does_not_inherit_a_completion_verdict(self):
        self.workspace(1, "ready_for_cleanup")
        self.world["workspaces"][0]["session_ids"] = ["new-session"]
        self.world["live_sessions"][0]["session_id"] = "new-session"
        html, _ = self.render()
        self.assertIn("Status unknown", html)
        self.assertNotIn("Check before closing", html)
        self.assertIn("earlier note has no matching session identity", html)

    def test_verified_live_context_survives_unverified_legacy_summary(self):
        self.workspace(1, workspace_id="old-workspace", summary="Wrong old summary.")
        self.world["live_sessions"][0].update({
            "last_assistant": {"ts": NOW.isoformat(), "text": "Choose a retry policy."},
            "last_user": {"ts": NOW.isoformat(), "text": "Fix the export issue."},
        })
        html, _ = self.render()
        self.assertIn("Latest recorded update", html)
        self.assertIn("lane-updates", Cards(html).cards["workspace:1"]["class"])
        self.assertIn("Choose a retry policy.", html)
        self.assertIn("Fix the export issue.", html)
        self.assertNotIn("Wrong old summary.", html)

    def test_old_recorded_session_is_nudged_without_claiming_original_creation(self):
        self.workspace(1, "needs_user")
        self.workspace(2, "active")
        self.world["live_sessions"][0]["first_recorded_at"] = "2026-09-01T10:00:00Z"
        self.world["live_sessions"][1]["first_recorded_at"] = "2026-08-01T10:00:00Z"
        html, _ = self.render()
        finish = html.split('id="finish-prompt"', 1)[1].split("</aside>", 1)[0]
        self.assertIn("Wrap up an older session", finish)
        self.assertIn("Task 1", finish)
        self.assertNotIn("Task 2", finish)
        self.assertIn("First recorded 2026-09-01", finish)
        self.assertNotIn("Created", finish)

    def test_pause_for_recycled_reference_does_not_park_current_work(self):
        self.workspace(1)
        self.write(".assistant/back-off.json", {
            "workspaces": [{"ws_ref": "workspace:1", "workspace_id": "old-workspace"}]})
        html, _ = self.render()
        self.assertIn("lane-needs-you", Cards(html).cards["workspace:1"]["class"])
        self.assertIn("Pause needs confirmation", html)

    def test_legacy_pause_suppresses_wrap_up_until_reconfirmed(self):
        self.workspace(1, "ready_for_cleanup")
        self.world["live_sessions"][0]["first_recorded_at"] = "2026-09-01T10:00:00Z"
        self.write(".assistant/back-off.json", {
            "workspaces": [{"ws_ref": "workspace:1", "reason": "Waiting on another team."}]})
        html, _ = self.render()
        self.assertIn("Pause needs confirmation", html)
        self.assertIn("Earlier pause reason (unverified)", html)
        self.assertIn("Waiting on another team.", html)
        self.assertNotIn("Wrap up an older session", html)
        self.assertNotIn("One task may be ready", html)

    def test_new_request_invalidates_previous_completion(self):
        self.workspace(1, "ready_for_cleanup", age=120)
        self.world["live_sessions"][0].update({
            "first_recorded_at": "2026-09-01T10:00:00Z",
            "last_user": {"ts": NOW.isoformat(), "text": "Also fix the retry loop."},
            "last_assistant": {"ts": (NOW - timedelta(seconds=120)).isoformat(),
                               "text": "The original task is complete."},
        })
        html, _ = self.render()
        self.assertIn("Request awaiting a response", html)
        self.assertNotIn("Check before closing", html)
        self.assertNotIn("Wrap up an older session", html)

    def test_new_blocker_replaces_current_summary_instead_of_hiding_response(self):
        for age in (120, 601):
            with self.subTest(age=age):
                self.world["workspaces"] = []
                self.world["live_sessions"] = []
                self.workspace(1, "ready_for_cleanup", age=age, summary="The old change is complete.")
                self.world["live_sessions"][0].update({
                    "last_assistant": {"ts": NOW.isoformat(), "text": "A new failure blocks this change."}})
                html, _ = self.render()
                self.assertIn("Latest recorded update", html)
                self.assertIn("A new failure blocks this change.", html)
                self.assertIn("Historical note (not current)", html)
                self.assertNotIn("Check before closing", html)

    def test_save_time_cannot_refresh_an_old_observation(self):
        self.workspace(1, "ready_for_cleanup", age=1000,
                       ts=NOW.timestamp(), last_updated_ts=NOW.timestamp())
        html, _ = self.render()
        self.assertIn("Status unknown", html)
        self.assertNotIn("Check before closing", html)

    def test_uuid_case_does_not_break_verified_context_or_pause(self):
        self.workspace(1)
        self.world["workspaces"][0]["workspace_id"] = "WORKSPACE-ID-1"
        self.world["workspaces"][0]["surfaces"][0]["surface_id"] = "SURFACE-ID-1"
        html, _ = self.render()
        self.assertIn("lane-working", Cards(html).cards["workspace:1"]["class"])
        self.write(".assistant/back-off.json", {
            "workspaces": [{"ws_ref": "workspace:1", "workspace_id": "workspace-id-1"}]})
        html, _ = self.render()
        self.assertIn("lane-parked", Cards(html).cards["workspace:1"]["class"])

    def test_oldest_pending_task_is_promoted_but_productive_and_parked_are_not(self):
        active = self.workspace(1)
        parked = self.workspace(2)
        self.write(".assistant/back-off.json", {
            "workspaces": [{"ws_ref": parked, "workspace_id": "workspace-id-2"}]})
        self.write(".claude/assistant-todo.json", {"items": [
            {"id": "td-1", "title": "Productive work", "status": "open",
             "createdAt": "2026-01-01", "dispatchedWs": active},
            {"id": "td-2", "title": "Paused work", "status": "blocked",
             "createdAt": "2026-01-02", "dispatchedWs": parked},
            {"id": "td-3", "title": "Finish the old fix", "status": "blocked",
             "createdAt": "2026-09-01"},
            {"id": "td-4", "title": "New idea", "status": "open",
             "createdAt": "2026-09-18T23:00:00Z"},
            {"id": "td-5", "title": "Already done", "status": "done",
             "createdAt": "2025-01-01"},
        ]})
        html, _ = self.render()
        self.assertIn("Finish the old fix", html)
        self.assertIn("Created 2026-09-01", html)
        self.assertNotIn("Productive work", html)
        self.assertNotIn("Paused work", html)
        self.assertNotIn("New idea", html)
        self.assertNotIn("Already done", html)

    def test_note_updates_are_not_treated_as_task_creation_dates(self):
        self.workspace(1, "needs_user", age=500)
        self.write(".claude/assistant-todo.json", {"items": [
            {"id": "td-1", "title": "Unknown age", "status": "open"}]})
        html, _ = self.render()
        self.assertNotIn("Created ", html)
        self.assertNotIn("older task", html)

    def test_old_stale_task_is_not_forgotten_by_the_finish_prompt(self):
        self.workspace(1, "active")
        self.write(".claude/assistant-todo.json", {"items": [
            {"id": "td-1", "title": "Review the forgotten task",
             "createdAt": "2026-08-01", "status": "stale"}]})
        html, _ = self.render()
        self.assertIn("Review the forgotten task", html)
        self.assertIn("Created 2026-08-01", html)

    def test_context_is_closed_and_extra_cards_are_collapsed(self):
        for number in range(12):
            self.workspace(number, "needs_user")
        html, count = self.render()
        self.assertEqual(count, 12)
        self.assertIn("Show 8 more", html)
        self.assertEqual(html.count('class="attention-context"'), 12)
        self.assertNotIn("<details open", html)

    def test_corrupt_context_is_reported_and_other_work_survives(self):
        self.workspace(1)
        (self.home / ".assistant/observer-summaries/bad.json").write_text("{bad")
        html, count = self.render()
        self.assertEqual(count, 1)
        self.assertIn("Context needs checking", html)
        self.assertIn("bad.json", html)

    def test_untrusted_text_is_escaped_everywhere(self):
        self.workspace(1, summary="<script>alert('context')</script>",
                       next="<img src=x onerror=alert('next')>")
        self.world["workspaces"][0]["title"] = "<svg onload=alert('title')>"
        self.write(".assistant/observer-summaries/workspace_1.json", {
            "ws_ref": "workspace:1", "title": "<svg onload=alert('title')>",
            "workspace_id": "workspace-id-1",
            "observation_complete": True,
            "observed_at": NOW.timestamp(),
            "observed_sessions": [{"surface_id": "surface-id-1", "provider": "claude",
                                   "session_id": "session-1"}],
            "verdict": "needs_user", "ts": NOW.timestamp(),
            "summary": "<script>alert('context')</script>",
            "next": "<img src=x onerror=alert('next')>",
        })
        html, _ = self.render()
        self.assertNotIn("<svg", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)

    def test_full_page_defaults_to_overview_and_keeps_existing_controls(self):
        self.workspace(1)
        self.write(".claude/cache/world.json", self.world)
        with mock.patch.object(self.renderer, "_cmux_workspaces", return_value={}):
            self.renderer.render()
        page = self.renderer.DASHBOARD_HTML.read_text()
        self.assertIn("|| '#overview'", page)
        for tab in ("overview", "brief", "decisions", "workspaces", "fleet", "connections", "todos"):
            self.assertIn(f'data-tab="{tab}"', page)
            self.assertIn(f'data-panel="{tab}"', page)
        self.assertIn("refreshDashboard", page)
        self.assertNotIn("location.reload()", page)

    def test_idle_context_with_matching_file_check_keeps_its_return_note(self):
        self.workspace(1, "needs_user", age=1200)
        session = self.world["live_sessions"][0]
        session.update({
            "context_built_at": (NOW - timedelta(days=2)).isoformat(),
            "context_checked_at": NOW.isoformat(),
            "guidance_context": {
                "source_version": "unchanged-transcript",
                "last_response": {"text": "The merged patch is saved.", "ts": (NOW - timedelta(days=2)).isoformat()},
                "last_request": {"text": "Complete the export fix.", "ts": (NOW - timedelta(days=3)).isoformat()},
                "pending_questions": [],
            },
        })
        self.write(".assistant/session-return-notes.json", {"sessions": [{
            "workspace_id": "workspace-id-1", "surface_id": "surface-id-1",
            "provider": "claude", "session_id": "session-1",
            "source_version": "unchanged-transcript", "goal": "Fix export",
            "progress": "The patch is merged and its record is saved.",
            "next_action": "Review closing the export-fix session.",
            "who": "user", "recommendation": "close_candidate",
            "completion_evidence": [{
                "kind": "pull_request", "state": "MERGED",
                "url": "https://github.com/example/project/pull/1",
            }],
        }]})
        html, _ = self.render()
        self.assertIn("lane-ready", Cards(html).cards["workspace:1"]["class"])
        self.assertIn("Review closing the export-fix session.", html)
        session["guidance_context"]["source_version"] = "new-work"
        html, _ = self.render()
        self.assertNotIn("lane-ready", Cards(html).cards["workspace:1"]["class"])
        self.assertNotIn("Review closing the export-fix session.", html)
