"""Check that missing or future approvals cannot partially expire a batch."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from assistant import decisions


class DecisionCleanupCoverageTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 1789820000

    def decision(self, name, now):
        record, _ = decisions.open_decision(
            event={"id": name, "source": "github", "external_id": name,
                   "kind": "review_requested", "title": name,
                   "refs": {"repo": "example/project", "pr": 10}},
            lane="escalate", policy_id="test", now=now)
        return record

    def change(self, record):
        return {"id": record["id"], "fingerprint": decisions.record_fingerprint(record),
                "note": "Approved historical alert cleanup, not completion of the work."}

    def test_missing_and_future_records_abort_the_whole_approved_batch(self):
        current = self.decision("current", self.now)
        future = self.decision("future", self.now + 1)
        missing = {**self.change(current), "id": "absent-approved-decision"}
        before_log = decisions.decisions_path().read_bytes()
        before_queue = decisions.queue_path().read_bytes()
        backup = self.home / "backups/approved-batch"
        for invalid, message in ((missing, "decision no longer exists"),
                                 (self.change(future), "decision timestamp is in the future")):
            with self.subTest(message=message), patch.object(
                    decisions, "_append_ledger", side_effect=AssertionError("must not broadcast")):
                with self.assertRaisesRegex(ValueError, message):
                    decisions.expire_selected(
                        [self.change(current), invalid], approval_id="approval",
                        backup_dir=backup, now=self.now)
                self.assertEqual(decisions.decisions_path().read_bytes(), before_log)
                self.assertEqual(decisions.queue_path().read_bytes(), before_queue)
                self.assertFalse(backup.exists())
                self.assertFalse(decisions.ledger_path().exists())
                self.assertEqual({row["id"] for row in decisions.open_decisions()},
                                 {current["id"], future["id"]})
        result = decisions.expire_selected(
            [self.change(current), self.change(future)], approval_id="approval",
            backup_dir=backup, now=self.now + 1)
        self.assertEqual(set(result["expired"]), {current["id"], future["id"]})
        self.assertEqual((backup / "decisions.jsonl").read_bytes(), before_log)
        self.assertEqual(decisions.open_decisions(), [])
        self.assertFalse(decisions.ledger_path().exists())
        self.assertFalse(result["outbound_notifications"])
