"""Check saved guidance against malformed inputs and real artifact boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from assistant.session_guidance import (
    guide_card,
    has_completion_evidence,
    matching_note,
    read_notes,
    response_text,
    text_excerpt,
)


class GuidanceCoverageTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.notes_path = self.home / "notes.json"
        self.note = {
            "workspace_id": "workspace", "surface_id": "surface",
            "provider": "claude", "session_id": "session", "source_version": "v1",
            "goal": "Verify the export.", "progress": "The export is verified.",
            "next_action": "Review the export.", "who": "user",
            "recommendation": "review",
        }
        self.session = {
            "workspace_id": "workspace", "surface_id": "surface",
            "provider": "claude", "session_id": "session",
            "guidance_context": {
                "source_version": "v1",
                "initial_request": {"text": "Export the image."},
                "last_request": {"text": "Verify the export."},
                "last_response": {"text": "The export is ready to inspect."},
                "pending_questions": [],
            },
        }
        self.card = {
            "lane": "updates", "state": "Review the last response",
            "action": "Read the response.", "request": "Original request",
            "next": "Original next step", "summary": "Original summary",
            "sessions": [self.session], "pause_uncertain": False,
            "wrap_eligible": False,
        }

    def saved_notes(self, notes):
        self.notes_path.write_text(json.dumps({"sessions": notes}))
        return read_notes(self.notes_path)

    def guide(self, notes=(), **options):
        saved, error = self.saved_notes(list(notes))
        self.assertEqual(error, "")
        arguments = {"snapshot_fresh": True, "tools_complete": True, "new_request": False}
        arguments.update(options)
        return guide_card(self.card, [self.session], saved, **arguments)

    def artifact(self, relative, content=b"Verified deliverable"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"kind": "artifact", "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest()}

    def test_missing_unreadable_and_invalid_documents_preserve_error_distinction(self):
        self.assertEqual(read_notes(self.notes_path), ([], ""))
        self.notes_path.mkdir()
        notes, error = read_notes(self.notes_path)
        self.assertEqual(notes, [])
        self.assertIn("couldn't be read", error)
        path = self.home / "other-notes.json"
        for document in (None, [], {}, {"sessions": {}}, {"sessions": None}):
            with self.subTest(document=document):
                path.write_text(json.dumps(document))
                self.assertEqual(read_notes(path),
                                 ([], "Your saved session notes have an unexpected format."))

    def test_malformed_notes_do_not_discard_valid_neighbors(self):
        invalid = [
            None, [], "not a note", {**self.note, "goal": " "},
            {**self.note, "provider": 7},
            {**self.note, "recommendation": "invented"},
            {**self.note, "who": "invented"},
            *({**self.note, "next_action": value} for value in (None, "", " ", 8)),
            {**self.note, "who": "nobody", "next_action": []},
            {**self.note, "completion_evidence": {}},
            {**self.note, "completion_evidence": [None]},
            {**self.note, "uncertainties": "unknown"},
            {**self.note, "uncertainties": [3]},
            {**self.note, "rationale": None},
            *({**self.note, "completion_evidence": [{key: value}]}
              for key in ("kind", "url", "path", "branch", "state")
              for value in (None, [], 2)),
            {**self.note, "completion_evidence": [{"url": "https://[broken"}]},
        ]
        for bad in invalid:
            with self.subTest(note=bad):
                notes, error = self.saved_notes([bad, self.note])
                self.assertEqual(notes, [self.note])
                self.assertEqual(error, "1 saved notes couldn't be used. Check their details.")

    def test_optional_fields_and_empty_evidence_are_valid(self):
        for evidence in ([], [{}], [{"url": ""}], [{"kind": "artifact", "path": "missing.md"}]):
            with self.subTest(evidence=evidence):
                note = {**self.note, "who": "unknown", "next_action": None,
                        "completion_evidence": evidence, "uncertainties": ["Not checked"]}
                self.assertEqual(self.saved_notes([note]), ([note], ""))

    def test_note_identity_requires_one_current_version(self):
        self.assertIs(matching_note(self.session, [self.note]), self.note)
        for notes in ([self.note, copy.deepcopy(self.note)],
                      [{**self.note, "source_version": "v2"}], []):
            with self.subTest(notes=notes):
                self.assertIsNone(matching_note(self.session, notes))
        for context in (None, {}, {"source_version": ""}):
            with self.subTest(context=context):
                self.assertIsNone(matching_note({**self.session, "guidance_context": context},
                                                [self.note]))

    def test_response_fallback_and_exact_excerpt_boundary(self):
        self.assertEqual(response_text({}), "")
        self.assertEqual(response_text({"last_assistant": {"text": "Older reply"}}), "Older reply")
        self.assertEqual(response_text({**self.session, "last_assistant": {"text": "Older reply"}}),
                         "The export is ready to inspect.")
        self.assertEqual(text_excerpt(" **Keep**\n  `this` text "), "Keep this text")
        self.assertEqual(text_excerpt("x" * 220), "x" * 220)
        self.assertEqual(text_excerpt("x" * 221), "x" * 217 + "...")
        self.assertEqual(text_excerpt("word longer", limit=8), "word...")

    def test_artifacts_require_exact_content_and_an_allowed_file(self):
        for suffix in ("md", "html"):
            with self.subTest(suffix=suffix):
                item = self.artifact(f"dev/generated-docs/result.{suffix}")
                self.assertTrue(has_completion_evidence({"completion_evidence": [item]}))
                Path(item["path"]).write_bytes(b"Changed after approval")
                self.assertFalse(has_completion_evidence({"completion_evidence": [item]}))
        for relative in ("dev/generated-docs-neighbor/result.md", "dev/result.md",
                         "dev/generated-docs/result.txt", "dev/generated-docs/result.MD"):
            with self.subTest(relative=relative):
                item = self.artifact(relative)
                self.assertFalse(has_completion_evidence({"completion_evidence": [item]}))
        item = self.artifact("dev/generated-docs/valid.md")
        for replacement in ({"path": str(self.home / "dev/generated-docs/missing.md")},
                            {"path": str(self.home / "dev/generated-docs")},
                            {"sha256": None}, {"sha256": ""}, {"sha256": 7}):
            with self.subTest(replacement=replacement):
                self.assertFalse(has_completion_evidence(
                    {"completion_evidence": [{**item, **replacement}]}))

    def test_symlink_cannot_escape_the_artifact_root(self):
        outside = self.artifact("outside.md")
        root = self.home / "dev/generated-docs"
        root.mkdir(parents=True)
        link = root / "linked.md"
        link.symlink_to(outside["path"])
        self.assertFalse(has_completion_evidence(
            {"completion_evidence": [{**outside, "path": str(link)}]}))

    def test_unreadable_artifact_downgrades_close_out_without_losing_the_note(self):
        item = self.artifact("dev/generated-docs/result.md")
        note = {**self.note, "recommendation": "close_candidate", "completion_evidence": [item]}
        original_open = Path.open

        def open_path(path, *args, **kwargs):
            if path == Path(item["path"]) and args == ("rb",):
                raise PermissionError("artifact access denied")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_path), self.assertLogs(
                "assistant.session_guidance", level="WARNING") as log:
            result = self.guide([note])
        self.assertIn("artifact access denied", log.output[0])
        self.assertEqual(result["lane"], "updates")
        self.assertFalse(result["wrap_eligible"])
        self.assertEqual(result["guidance_note"], note)
        self.assertEqual(result["state"], "Close-out evidence needs rechecking")

    def test_completion_evidence_skips_bad_entries_and_checks_pull_request_origin(self):
        good = {"kind": "pull_request", "state": "MERGED",
                "url": "https://github.com/example/project/pull/42"}
        invalid = [
            None, {}, {"kind": "artifact", "path": None},
            {**good, "kind": "issue"}, {**good, "state": "OPEN"},
            *({**good, "url": url} for url in (
                [], "https://[broken", "", None, "http://github.com/example/project/pull/42",
                "https://github.example.com/example/project/pull/42",
                "https://github.com@example.com/example/project/pull/42",
                "https://github.com/example/project/issues/42")),
        ]
        for item in invalid:
            with self.subTest(item=item):
                self.assertFalse(has_completion_evidence({"completion_evidence": [item]}))
                self.assertTrue(has_completion_evidence({"completion_evidence": [item, good]}))
        for evidence in (None, {}, "merged"):
            with self.subTest(evidence=evidence):
                self.assertFalse(has_completion_evidence({"completion_evidence": evidence}))

    def test_stale_paused_and_unknown_sessions_cannot_offer_new_guidance(self):
        stale = self.guide([self.note], snapshot_fresh=False)
        self.assertEqual(stale["lane"], "unknown")
        self.assertFalse(stale["wrap_eligible"])
        self.assertIsNone(stale["guidance_note"])
        for overrides in ({"lane": "parked"}, {"pause_uncertain": True}):
            with self.subTest(overrides=overrides):
                card = {**self.card, **overrides}
                result = guide_card(card, [self.session], [self.note],
                                    snapshot_fresh=True, tools_complete=True, new_request=False)
                self.assertEqual(result["lane"], card["lane"])
                self.assertEqual(result["action"], card["action"])
                self.assertIsNone(result["guidance_note"])
        self.session["guidance_context"] = None
        result = self.guide([self.note])
        self.assertEqual(result["lane"], "unknown")
        self.assertEqual(result["request"], self.card["request"])
        self.assertFalse(result["wrap_eligible"])

    def test_question_note_requires_one_session_and_a_user_answer(self):
        question = {"question": "Publish the export?", "options": [{"label": "Publish"}]}
        self.session["guidance_context"]["pending_questions"] = [question]
        for who, recommendation, applies in (
            ("user", "answer", True), ("user", "continue", False), ("agent", "answer", False),
        ):
            with self.subTest(who=who, recommendation=recommendation):
                note = {**self.note, "who": who, "recommendation": recommendation}
                result = self.guide([note], tools_complete=False)
                self.assertEqual(result["action"], note["next_action"] if applies else question["question"])
                self.assertEqual(result["guidance_note"], note if applies else None)
                self.assertEqual(result["questions"], [question])
                self.assertFalse(result["wrap_eligible"])
        self.card["sessions"].append({**self.session, "session_id": "another"})
        result = self.guide([{**self.note, "recommendation": "answer"}])
        self.assertEqual(result["action"], question["question"])
        self.assertIsNone(result["guidance_note"])

    def test_saved_note_routing_respects_owner_and_running_tools(self):
        cases = [
            ("unknown", "review", True, "unknown", "Next step not established", False),
            ("nobody", "park", True, "updates", "No action for you", False),
            ("agent", "answer", True, "working", "Agent's next step", False),
            ("user", "continue", True, "working", "Agent's next step", False),
            ("user", "answer", True, "needs-you", "Your next step", True),
            ("user", "review", True, "needs-you", "Your next step", True),
            ("user", "answer", False, "updates", "Latest recorded update", False),
            ("user", "park", True, "needs-you", "Ready to park deliberately", True),
            ("user", "park", False, "updates", "Latest recorded update", False),
            ("user", "unknown", True, "updates", "Latest recorded update", False),
        ]
        for who, recommendation, complete, lane, state, eligible in cases:
            with self.subTest(who=who, recommendation=recommendation, complete=complete):
                note = {**self.note, "who": who, "recommendation": recommendation}
                result = self.guide([note], tools_complete=complete)
                self.assertEqual((result["lane"], result["state"], result["wrap_eligible"]),
                                 (lane, state, eligible))
                self.assertEqual(result["guidance_note"], note)
                self.assertEqual(result["goal"], note["goal"])
        self.card["lane"] = "working"
        for recommendation, state in (("review", "Tools still running"),
                                      ("continue", "Agent's next step")):
            with self.subTest(recommendation=recommendation):
                result = self.guide([{**self.note, "recommendation": recommendation}])
                self.assertEqual((result["lane"], result["state"], result["wrap_eligible"]),
                                 ("working", state, False))

    def test_current_unknown_observation_stays_unknown(self):
        self.card.update(observation_current=True, state="Tool status unknown")
        result = self.guide()
        self.assertEqual(result["lane"], "unknown")
        self.assertFalse(result["wrap_eligible"])
        self.assertEqual(result["action"], self.card["action"])

    def test_unreviewed_running_work_uses_reply_request_or_waiting_fallback(self):
        self.card["lane"] = "working"
        result = self.guide()
        self.assertEqual(result["action"], "The export is ready to inspect.")
        self.session["guidance_context"]["last_response"] = None
        result = self.guide()
        self.assertEqual(result["action"], "Verify the export.")
        self.session["guidance_context"]["last_request"] = None
        self.card["request"] = ""
        result = self.guide()
        self.assertEqual(result["action"], "Your assistant is waiting for a result.")
        self.assertFalse(result["wrap_eligible"])

    def test_new_request_cannot_reuse_saved_advice_and_does_not_mutate_inputs(self):
        before = copy.deepcopy((self.card, self.session, self.note))
        result = self.guide([self.note], new_request=True)
        self.assertEqual(result["lane"], "updates")
        self.assertEqual(result["action"], "Verify the export.")
        self.assertEqual(result["source_kind"], "Your last request")
        self.assertIsNone(result["guidance_note"])
        self.assertFalse(result["wrap_eligible"])
        self.assertEqual((self.card, self.session, self.note), before)
