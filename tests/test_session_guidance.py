"""Check specific return notes without turning missing evidence into user decisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from assistant.session_guidance import guide_card, matching_note, read_notes


class SessionGuidanceTests(TestCase):
    def setUp(self):
        self.session = {
            "workspace_id": "workspace-uuid", "surface_id": "surface-uuid",
            "provider": "claude", "session_id": "session-A", "pending_tool_use": False,
            "guidance_context": {
                "source_version": "version-one",
                "initial_request": {"text": "Repair the viewport export."},
                "last_request": {"text": "Verify the patch."},
                "last_response": {"text": "The loading-overlay check needs a deterministic initialization hold."},
                "pending_questions": [],
            },
        }
        self.card = {
            "lane": "needs-you", "state": "Review the last response",
            "action": "Read the last response.", "request": "Verify the patch.",
            "next": "Check the current response.", "summary": "Old short preview",
            "sessions": [self.session], "pause_uncertain": False,
            "wrap_eligible": True,
        }
        self.note = {
            "workspace_id": "WORKSPACE-UUID", "surface_id": "SURFACE-UUID",
            "provider": "claude", "session_id": "session-A", "source_version": "version-one",
            "goal": "Repair the viewport export", "progress": "The patch is merged.",
            "next_action": "Save the merged change link, then review closing this session.",
            "who": "user", "recommendation": "close_candidate",
            "completion_evidence": [{"kind": "pull_request", "url": "https://github.com/example/repo/pull/3",
                                     "state": "MERGED"}],
        }

    def guide(self, notes=None, **overrides):
        return guide_card(self.card, [self.session], [] if notes is None else notes,
                          **{"snapshot_fresh": True, "tools_complete": True,
                             "new_request": False, **overrides})

    def test_unreviewed_reply_is_an_update_not_a_user_decision(self):
        result = self.guide()
        self.assertEqual(result["lane"], "updates")
        self.assertIn("deterministic initialization hold", result["action"])
        self.assertFalse(result["wrap_eligible"])

    def test_pending_question_displays_actual_question_and_choices(self):
        question = {
            "tool_use_id": "question-1", "question": "Keep the playback fix without changing mid-drag split?",
            "options": [{"label": "Playback only"}, {"label": "Include mid-drag"}],
        }
        self.session["guidance_context"]["pending_questions"] = [question]
        self.card["lane"] = "working"
        result = self.guide(tools_complete=False)
        self.assertEqual(result["lane"], "needs-you")
        self.assertEqual(result["action"], question["question"])
        self.assertEqual(result["questions"], [question])
        self.assertFalse(result["wrap_eligible"])

    def test_reviewed_unchanged_note_gives_a_specific_close_out(self):
        result = self.guide([self.note])
        self.assertEqual(result["lane"], "ready")
        self.assertEqual(result["action"], self.note["next_action"])
        self.assertEqual(result["goal"], self.note["goal"])
        self.assertTrue(result["wrap_eligible"])

    def test_changed_source_invalidates_note_without_hiding_new_reply(self):
        self.session["guidance_context"]["source_version"] = "new-version"
        result = self.guide([self.note])
        self.assertEqual(result["lane"], "updates")
        self.assertNotIn("patch is merged", result["summary"])
        self.assertIn("loading-overlay", result["summary"])

    def test_identity_and_provider_session_id_must_match(self):
        for key, value in (("workspace_id", "another-workspace"), ("surface_id", "another-surface"),
                           ("provider", "droid"), ("session_id", "session-a")):
            with self.subTest(key=key):
                self.assertIsNone(matching_note(self.session, [{**self.note, key: value}]))

    def test_unknown_tool_state_never_promotes_close_out(self):
        result = self.guide([self.note], tools_complete=False)
        self.assertEqual(result["lane"], "updates")
        self.assertFalse(result["wrap_eligible"])

    def test_unverified_completion_claim_cannot_promote_close_out(self):
        for evidence in ("claimed merged", [{"state": "MERGED"}],
                         [{"kind": "pull_request", "state": "OPEN",
                           "url": "https://github.com/example/repo/pull/3"}]):
            with self.subTest(evidence=evidence):
                result = self.guide([{**self.note, "completion_evidence": evidence}])
                self.assertNotEqual(result["lane"], "ready")
                self.assertFalse(result["wrap_eligible"])

    def test_plan_only_close_out_requires_the_exact_preserved_artifact(self):
        with TemporaryDirectory() as directory, patch("pathlib.Path.home", return_value=Path(directory)):
            path = Path(directory) / "dev/generated-docs/delivered-plan.md"
            path.parent.mkdir(parents=True)
            path.write_text("A delivered plan, not an implemented feature.")
            note = {**self.note, "completion_evidence": [{
                "kind": "artifact", "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }]}
            self.assertEqual(self.guide([note])["lane"], "ready")
            path.write_text("Changed since review.")
            self.assertNotEqual(self.guide([note])["lane"], "ready")

    def test_waiting_for_agent_work_is_not_a_human_decision(self):
        note = {**self.note, "who": "agent", "recommendation": "continue",
                "progress": "The coder is implementing the permission gate.",
                "next_action": "Wait for the coder, then run the deferral mutation checks."}
        result = self.guide([note])
        self.assertEqual(result["lane"], "working")
        self.assertEqual(result["action"], note["next_action"])
        self.assertFalse(result["wrap_eligible"])

    def test_missing_context_is_separate_from_needs_you(self):
        result = guide_card(self.card, [], [], snapshot_fresh=True,
                            tools_complete=False, new_request=False)
        self.assertEqual(result["lane"], "unknown")
        self.assertFalse(result["wrap_eligible"])

    def test_new_user_request_does_not_reuse_older_guidance(self):
        result = self.guide([self.note], new_request=True)
        self.assertEqual(result["lane"], "updates")
        self.assertEqual(result["action"], "Verify the patch.")
        self.assertFalse(result["wrap_eligible"])

    def test_partial_multi_session_guidance_cannot_close_workspace(self):
        other = {**self.session, "surface_id": "other", "session_id": "other"}
        self.card["sessions"].append(other)
        result = guide_card(self.card, [self.session, other], [self.note], snapshot_fresh=True,
                            tools_complete=True, new_request=False)
        self.assertFalse(result["wrap_eligible"])

    def test_bad_notes_report_errors_without_dropping_good_notes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "notes.json"
            path.write_text(json.dumps({"sessions": [
                self.note, {"goal": "incomplete"},
                {**self.note, "recommendation": []}, {**self.note, "who": []},
            ]}))
            notes, error = read_notes(path)
            self.assertEqual(notes, [self.note])
            self.assertIn("3 invalid", error)
            path.write_text("{broken")
            notes, error = read_notes(path)
            self.assertEqual(notes, [])
            self.assertIn("could not be read", error)

    def test_current_observer_note_does_not_lose_its_progress_to_raw_text(self):
        self.card.update(observation_current=True, summary="Verified progress.",
                         next="Run the measured regression case.")
        result = self.guide()
        self.assertEqual(result["summary"], "Verified progress.")
        self.assertEqual(result["next"], "Run the measured regression case.")

    def test_no_next_action_is_valid_for_an_explicit_no_action_note(self):
        note = {**self.note, "who": "nobody", "recommendation": "park", "next_action": None}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "notes.json"
            path.write_text(json.dumps({"sessions": [note]}))
            notes, error = read_notes(path)
            self.assertEqual(error, "")
            result = self.guide(notes)
            self.assertEqual(result["lane"], "updates")
            self.assertFalse(result["wrap_eligible"])
