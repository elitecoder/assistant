"""Exercise approved cleanup against real decision files without outgoing events."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from assistant import decisions


NOW = 1789820000


class BacklogCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.backup = self.home / "backups/approved-batch"

    def decision(self, name):
        record, _ = decisions.open_decision(
            event={"id": name, "source": "github", "external_id": name,
                   "kind": "review_requested", "title": name,
                   "refs": {"repo": "example/project", "pr": 10}},
            lane="escalate", policy_id="test", now=NOW,
        )
        return record

    def change(self, record):
        return {"id": record["id"], "fingerprint": decisions.record_fingerprint(record),
                "note": "Historical alert; live PR closure checked. Work is not marked complete."}

    def apply(self, changes):
        return decisions.expire_selected(
            changes, approval_id="approved-2026-09-19", backup_dir=self.backup, now=NOW + 10)

    def test_expires_only_approved_records_and_preserves_history(self):
        one = self.decision("retire-one")
        two = self.decision("retire-two")
        keep = self.decision("keep-current-question")
        before = decisions.decisions_path().read_bytes()
        with mock.patch.object(decisions, "_append_ledger",
                               side_effect=AssertionError("must not broadcast cleanup")):
            result = self.apply([self.change(one), self.change(two)])
        latest = decisions.fold(decisions.read_log())
        self.assertEqual(set(result["expired"]), {one["id"], two["id"]})
        self.assertEqual(latest[keep["id"]]["status"], "open")
        self.assertEqual(latest[one["id"]]["status"], "expired")
        self.assertEqual(latest[one["id"]]["created_epoch"], one["created_epoch"])
        self.assertEqual((self.backup / "decisions.jsonl").read_bytes(), before)
        manifest = json.loads((self.backup / "approval.json").read_text())
        self.assertEqual(manifest["source_log_sha256"], hashlib.sha256(before).hexdigest())
        self.assertFalse(result["outbound_notifications"])
        self.assertFalse(decisions.ledger_path().exists())
        self.assertFalse((self.home / ".claude/assistant-todo.json").exists())
        self.assertEqual([row["id"] for row in decisions.open_decisions()], [keep["id"]])

    def test_changed_record_aborts_entire_batch_before_writes(self):
        one = self.decision("one")
        two = self.decision("two")
        plan = [self.change(one), self.change(two)]
        decisions.transition(two["id"], "accepted", via="user", now=NOW + 1)
        before = decisions.decisions_path().read_bytes()
        with self.assertRaisesRegex(ValueError, "changed since approval"):
            self.apply(plan)
        self.assertEqual(decisions.decisions_path().read_bytes(), before)
        self.assertFalse(self.backup.exists())
        self.assertEqual(decisions.fold(decisions.read_log())[one["id"]]["status"], "open")

    def test_unrelated_new_records_do_not_invalidate_approved_subset(self):
        chosen = self.decision("chosen")
        plan = [self.change(chosen)]
        unrelated = self.decision("new-unrelated-question")
        self.apply(plan)
        self.assertEqual(decisions.fold(decisions.read_log())[unrelated["id"]]["status"], "open")

    def test_retry_does_not_append_again_or_send_notifications(self):
        chosen = self.decision("chosen")
        plan = [self.change(chosen)]
        self.apply(plan)
        before = decisions.decisions_path().read_bytes()
        result = self.apply(plan)
        self.assertEqual(result["expired"], [])
        self.assertEqual(result["already_expired"], [chosen["id"]])
        self.assertEqual(decisions.decisions_path().read_bytes(), before)
        self.assertFalse(decisions.ledger_path().exists())

    def test_retries_cannot_claim_another_backup(self):
        chosen = self.decision("chosen")
        plan = [self.change(chosen)]
        self.apply(plan)
        with self.assertRaisesRegex(ValueError, "original backup"):
            decisions.expire_selected(plan, approval_id="approved-2026-09-19",
                                      backup_dir=self.home / "other", now=NOW + 11)

    def test_retry_cannot_rewrite_receipt_for_a_different_subset(self):
        one = self.decision("one")
        two = self.decision("two")
        plan = [self.change(one), self.change(two)]
        self.apply(plan)
        before = (self.backup / "result.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "different approval"):
            self.apply(plan[:1])
        self.assertEqual((self.backup / "result.json").read_bytes(), before)

    def test_preserves_previous_rotation_without_rewriting_it(self):
        chosen = self.decision("chosen")
        rotated = decisions.decisions_path().with_name("decisions.jsonl.1")
        rotated.write_bytes(b"older archived history\n")
        with mock.patch.object(decisions, "MAX_LOG_BYTES", 1), mock.patch.object(
                decisions, "_write_queue", wraps=decisions._write_queue) as rebuild:
            self.apply([self.change(chosen)])
        self.assertEqual((self.backup / "decisions.jsonl.1").read_bytes(),
                         b"older archived history\n")
        self.assertEqual(rotated.read_bytes(), b"older archived history\n")
        self.assertEqual(rebuild.call_count, 1)

    def test_missing_final_newline_does_not_lose_an_unrelated_record(self):
        chosen = self.decision("chosen")
        keep = self.decision("last-record-without-newline")
        path = decisions.decisions_path()
        before = path.read_bytes().rstrip(b"\n")
        path.write_bytes(before)
        self.apply([self.change(chosen)])
        current = decisions.fold(decisions.read_log())
        self.assertEqual(current[keep["id"]]["status"], "open")
        self.assertEqual(current[chosen["id"]]["status"], "expired")
        self.assertEqual((self.backup / "decisions.jsonl").read_bytes(), before)

    def test_interrupted_staging_never_publishes_a_partial_batch(self):
        one = self.decision("one")
        two = self.decision("two")
        keep = self.decision("keep")
        plan = [self.change(one), self.change(two)]
        before = decisions.decisions_path().read_bytes()
        queue_before = decisions.queue_path().read_bytes()
        original = json.dumps
        expired_writes = []

        def encode(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("status") == "expired":
                expired_writes.append(value["id"])
                if len(expired_writes) == 2:
                    raise OSError("interrupted batch write")
            return original(value, *args, **kwargs)

        with mock.patch.object(decisions.json, "dumps", encode):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertEqual(len(expired_writes), 2)
        self.assertEqual(decisions.decisions_path().read_bytes(), before)
        self.assertEqual(decisions.queue_path().read_bytes(), queue_before)
        result = self.apply(plan)
        self.assertEqual(set(result["expired"]), {one["id"], two["id"]})
        self.assertEqual(decisions.fold(decisions.read_log())[keep["id"]]["status"], "open")

    def test_invalid_and_duplicate_plans_do_not_write(self):
        record = self.decision("chosen")
        change = self.change(record)
        before = decisions.decisions_path().read_bytes()
        for plan in ([], [None], [{"id": []}], [change, change]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                self.apply(plan)
        self.assertEqual(decisions.decisions_path().read_bytes(), before)

    def test_corrupt_history_is_not_silently_compacted_away(self):
        record = self.decision("chosen")
        with decisions.decisions_path().open("a") as stream:
            stream.write("{broken\n")
        before = decisions.decisions_path().read_bytes()
        with self.assertRaisesRegex(ValueError, "unreadable"):
            self.apply([self.change(record)])
        self.assertEqual(decisions.decisions_path().read_bytes(), before)

    def test_backup_failure_prevents_state_changes(self):
        record = self.decision("chosen")
        before = decisions.decisions_path().read_bytes()
        with mock.patch.object(decisions.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply([self.change(record)])
        self.assertEqual(decisions.decisions_path().read_bytes(), before)

    def test_receipt_failure_can_resume_from_the_original_backup(self):
        record = self.decision("chosen")
        plan = [self.change(record)]
        original = Path.write_text

        def write(path, *args, **kwargs):
            if path.name == "result.json.tmp":
                raise OSError("receipt write failed")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", write):
            with self.assertRaises(OSError):
                self.apply(plan)
        result = self.apply(plan)
        self.assertEqual(result["already_expired"], [record["id"]])
        self.assertTrue((self.backup / "result.json").exists())
