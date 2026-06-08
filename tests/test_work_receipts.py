"""Integration tests for the work-receipt system.

Drives the REAL bin/tools/write-receipt.py and bin/pre-cleanup-check.py with a
sandboxed HOME (every HOME-derived path constant is computed at import time, so
each test imports a fresh module bound to its own tempdir). No mocking of the
filesystem — receipts are written and read back off disk, and the JSONL log is
appended for real.

Coverage:
  - write_receipt creates the canonical receipt file with the right fields
  - write_receipt appends every receipt to work-receipts.jsonl
  - pre_cleanup_check returns gate=pass when a receipt exists
  - pre_cleanup_check returns gate=block when none exists
  - quality_score: CI green + approved = high; partial cases = medium/low
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
WRITE_RECEIPT_PATH = REPO / "bin/tools/write-receipt.py"
PRE_CLEANUP_PATH = REPO / "bin/pre-cleanup-check.py"


def _load(name: str, path: Path, home: Path):
    """Import a bin script with HOME pointed at `home`. Returns a fresh module
    bound to that home — every path constant (RECEIPTS_DIR, LOG_PATH) is
    computed at import, so each test gets clean, isolated paths."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WriteReceiptTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._home = Path(self._tmp_obj.name)
        # write-receipt shells out to `cmux list-workspaces` when --project is
        # absent; we always pass --project in tests, so it never runs.
        self.wr = _load("write_receipt_mod", WRITE_RECEIPT_PATH, self._home)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_write_receipt_creates_file(self):
        receipt = self.wr.write_receipt(
            ws_ref="workspace:43", project="Connections P8 split parity",
            pr=11034, ci_status="green", reviewer_approved=True,
            test_count=14, summary="14 AUG scenarios, all CI green",
            outcome="shipped",
        )
        rdir = self._home / ".assistant/receipts"
        files = list(rdir.glob("workspace-43-*.json"))
        self.assertEqual(len(files), 1, "exactly one receipt file written")

        on_disk = json.loads(files[0].read_text())
        self.assertEqual(on_disk["ws_ref"], "workspace:43")
        self.assertEqual(on_disk["project"], "Connections P8 split parity")
        self.assertEqual(on_disk["pr_number"], 11034)
        self.assertIn("pull/11034", on_disk["pr_url"])
        self.assertEqual(on_disk["ci_status"], "green")
        self.assertIs(on_disk["reviewer_approved"], True)
        self.assertEqual(on_disk["test_count"], 14)
        self.assertEqual(on_disk["outcome"], "shipped")
        self.assertEqual(on_disk["quality_score"], "high")
        # Slug uses a dash, never a colon (filesystem-safe).
        self.assertNotIn(":", files[0].name)
        # No leftover tmp file from the atomic write.
        self.assertEqual(list(rdir.glob("*.tmp")), [])

    def test_write_receipt_appends_to_log(self):
        self.wr.write_receipt(
            ws_ref="workspace:1", project="A", pr=None, ci_status="green",
            reviewer_approved=True, test_count=None, summary="first",
            outcome="shipped",
        )
        self.wr.write_receipt(
            ws_ref="workspace:2", project="B", pr=None, ci_status="red",
            reviewer_approved=False, test_count=None, summary="second",
            outcome="abandoned",
        )
        log_path = self._home / "dev/generated-docs/work-receipts.jsonl"
        self.assertTrue(log_path.exists())
        lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["project"], "A")
        self.assertEqual(lines[1]["project"], "B")
        self.assertEqual(lines[0]["quality_score"], "high")
        self.assertEqual(lines[1]["quality_score"], "low")

    def test_pr_url_none_when_no_pr(self):
        receipt = self.wr.write_receipt(
            ws_ref="workspace:5", project="no-pr work", pr=None,
            ci_status="unknown", reviewer_approved=None, test_count=None,
            summary="audit, no PR", outcome="closed-no-pr",
        )
        self.assertIsNone(receipt["pr_url"])
        self.assertIsNone(receipt["pr_number"])

    def test_quality_score_computation(self):
        f = self.wr.compute_quality_score
        # high: CI green AND reviewer approved
        self.assertEqual(f("green", True, "shipped"), "high")
        # medium: CI green OR reviewer approved (partial)
        self.assertEqual(f("green", None, "shipped"), "medium")
        self.assertEqual(f("green", False, "shipped"), "medium")
        self.assertEqual(f("unknown", True, "shipped"), "medium")
        # low: CI red OR abandoned (red beats a partial-green)
        self.assertEqual(f("red", True, "shipped"), "low")
        self.assertEqual(f("green", True, "abandoned"), "low")
        # low: nothing positive known
        self.assertEqual(f("unknown", None, "closed-no-pr"), "low")
        self.assertEqual(f("unknown", False, "shipped"), "low")


class PreCleanupCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._home = Path(self._tmp_obj.name)
        self.wr = _load("write_receipt_mod2", WRITE_RECEIPT_PATH, self._home)
        self.gate = _load("pre_cleanup_mod", PRE_CLEANUP_PATH, self._home)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_pre_cleanup_check_pass(self):
        # A receipt on disk → gate opens, and surfaces the receipt path + the
        # receipt's own summary as evidence.
        self.wr.write_receipt(
            ws_ref="workspace:7", project="P", pr=900, ci_status="green",
            reviewer_approved=True, test_count=3, summary="all green",
            outcome="shipped",
        )
        result = self.gate.pre_cleanup_check("workspace:7")
        self.assertEqual(result["gate"], "pass")
        self.assertIn("workspace-7-", result["receipt_path"])
        self.assertTrue(Path(result["receipt_path"]).exists())
        self.assertEqual(result["evidence"], "all green")

    def test_pre_cleanup_check_block(self):
        # No receipt anywhere → gate blocks with a no-receipt reason.
        result = self.gate.pre_cleanup_check("workspace:404")
        self.assertEqual(result["gate"], "block")
        self.assertEqual(result["reason"], "no receipt")
        self.assertIn("workspace:404", result["evidence"])

    def test_pre_cleanup_check_picks_newest_receipt(self):
        # Two receipts for one ws → newest by mtime wins.
        rdir = self._home / ".assistant/receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        old = rdir / "workspace-9-1700000000.json"
        old.write_text(json.dumps({"ws_ref": "workspace:9", "summary": "old"}))
        new = rdir / "workspace-9-1700009999.json"
        new.write_text(json.dumps({"ws_ref": "workspace:9", "summary": "new"}))
        # Force mtime ordering deterministically.
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_700_009_999, 1_700_009_999))
        result = self.gate.pre_cleanup_check("workspace:9")
        self.assertEqual(result["gate"], "pass")
        self.assertEqual(result["evidence"], "new")

    def test_pre_cleanup_check_gate_error_on_latest_receipt_exception(self):
        # If latest_receipt raises (e.g. glob explodes), fail safe to block.
        from unittest import mock
        with mock.patch.object(self.gate, "latest_receipt",
                               side_effect=OSError("disk error")):
            result = self.gate.pre_cleanup_check("workspace:99")
        self.assertEqual(result["gate"], "block")
        self.assertEqual(result["reason"], "gate error")
        self.assertIn("disk error", result["evidence"])

    def test_pre_cleanup_check_malformed_receipt_still_passes(self):
        # A receipt file that isn't valid JSON still lets the gate pass —
        # the evidence is just empty.
        rdir = self._home / ".assistant/receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "workspace-55-1234567890.json").write_text("{ not json")
        result = self.gate.pre_cleanup_check("workspace:55")
        self.assertEqual(result["gate"], "pass")
        self.assertEqual(result["evidence"], "")

    def test_main_returns_zero_and_emits_json(self):
        # main() always exits 0 and emits valid JSON.
        import io, sys
        from unittest import mock
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = self.gate.main(["--ws", "workspace:999"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertIn("gate", data)


if __name__ == "__main__":
    unittest.main()
