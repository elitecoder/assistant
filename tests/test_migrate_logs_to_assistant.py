"""Tests for bin/migrate-logs-to-assistant.sh — the one-off migration that
relocates Assistant LaunchAgent logs from ~/.architect/orchestrator-logs/ to
~/.assistant/logs/.

The script is driven via subprocess against a sandboxed HOME so the real user
state is never touched. We pass --no-launchctl-hint everywhere so the test never
shells out to the real `launchctl` (the hint block is print-only anyway, but
suppressing it keeps the tests hermetic).

Coverage:
  - dry-run changes nothing and prints a plan
  - --apply moves ONLY Assistant-owned files, leaving orchestrator logs in place
  - re-running --apply is a no-op (idempotency)
  - no-clobber: a pre-existing destination is preserved; the source is parked
    beside it as <dest>.from-architect-<ts>
  - a missing source dir is a clean no-op
  - every applied move is recorded in the migrate log
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/migrate-logs-to-assistant.sh"

# The 14 launchd captures + 3 in-code app logs the script owns.
ASSISTANT_OWNED = [
    "assistant-pulse.launchd.out", "assistant-pulse.launchd.err",
    "assistant-page.launchd.out", "assistant-page.launchd.err",
    "session-context-watcher.launchd.out", "session-context-watcher.launchd.err",
    "workspace-watcher.launchd.out", "workspace-watcher.launchd.err",
    "slack-reactor.launchd.out", "slack-reactor.launchd.err",
    "world-scanner.launchd.out", "world-scanner.launchd.err",
    "assistant-daemon.launchd.out", "assistant-daemon.launchd.err",
    "world-scanner.out", "session-context-watcher.out", "session-context-watcher.err",
]

# Files in the same dir that belong to the orchestrator, NOT the Assistant.
ORCHESTRATOR_OWNED = [
    "work-dispatcher.out", "work-dispatcher.err",
    "hermes-executor.out", "world-evaluator.launchd.err",
]


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--no-launchctl-hint", *args],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )


def seed(home: Path, names: list[str]) -> Path:
    old = home / ".architect/orchestrator-logs"
    old.mkdir(parents=True, exist_ok=True)
    for n in names:
        (old / n).write_text(f"content-{n}\n")
    return old


class TestMigrateLogs(unittest.TestCase):

    def test_dry_run_changes_nothing(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            old = seed(home, ASSISTANT_OWNED + ORCHESTRATOR_OWNED)
            before = sorted(p.name for p in old.iterdir())

            res = run(home)  # dry-run is the default

            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("DRY RUN", res.stdout)
            # Nothing moved: source dir unchanged, dest dir not populated.
            self.assertEqual(sorted(p.name for p in old.iterdir()), before)
            new = home / ".assistant/logs"
            moved = [p for p in new.iterdir()] if new.exists() else []
            self.assertEqual(moved, [], "dry-run must not move any file")

    def test_apply_moves_only_assistant_owned(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            old = seed(home, ASSISTANT_OWNED + ORCHESTRATOR_OWNED)

            res = run(home, "--apply")
            self.assertEqual(res.returncode, 0, res.stderr)

            new = home / ".assistant/logs"
            # Every Assistant-owned file landed in the new dir...
            for n in ASSISTANT_OWNED:
                self.assertTrue((new / n).exists(), f"{n} should have been moved")
                self.assertFalse((old / n).exists(), f"{n} should be gone from source")
            # ...and every orchestrator-owned file stayed put.
            for n in ORCHESTRATOR_OWNED:
                self.assertTrue((old / n).exists(), f"{n} must NOT be moved")
                self.assertFalse((new / n).exists(), f"{n} must NOT appear in dest")

    def test_apply_is_idempotent(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            seed(home, ASSISTANT_OWNED)

            first = run(home, "--apply")
            self.assertEqual(first.returncode, 0, first.stderr)
            new = home / ".assistant/logs"
            snapshot = {p.name: p.read_text() for p in new.iterdir() if p.is_file()}

            second = run(home, "--apply")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("0 to move", second.stdout)
            after = {p.name: p.read_text() for p in new.iterdir() if p.is_file()}
            self.assertEqual(snapshot, after, "second run must not change anything")

    def test_no_clobber_preserves_existing_dest(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            seed(home, ["world-scanner.out"])
            new = home / ".assistant/logs"
            new.mkdir(parents=True, exist_ok=True)
            (new / "world-scanner.out").write_text("PRE-EXISTING-DEST\n")

            res = run(home, "--apply")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("COLLIDE", res.stdout)

            # Original destination content is untouched.
            self.assertEqual((new / "world-scanner.out").read_text(), "PRE-EXISTING-DEST\n")
            # Source content is parked alongside, not lost.
            parked = list(new.glob("world-scanner.out.from-architect-*"))
            self.assertEqual(len(parked), 1, "source must be parked, not dropped")
            self.assertEqual(parked[0].read_text(), "content-world-scanner.out\n")
            # Source is gone from the old dir.
            self.assertFalse((home / ".architect/orchestrator-logs/world-scanner.out").exists())

    def test_missing_source_dir_is_noop(self):
        with TemporaryDirectory() as td:
            home = Path(td)  # no .architect/orchestrator-logs at all

            res = run(home, "--apply")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("nothing to migrate", res.stdout)
            self.assertIn("0 to move", res.stdout)

    def test_apply_records_moves_in_migrate_log(self):
        with TemporaryDirectory() as td:
            home = Path(td)
            seed(home, ["assistant-pulse.launchd.out", "world-scanner.out"])

            res = run(home, "--apply")
            self.assertEqual(res.returncode, 0, res.stderr)

            ledger = home / ".assistant/logs/migrate-logs-to-assistant.log"
            self.assertTrue(ledger.exists(), "an applied migration must write a ledger")
            text = ledger.read_text()
            self.assertIn("assistant-pulse.launchd.out", text)
            self.assertIn("world-scanner.out", text)
            self.assertIn("MOVE", text)


if __name__ == "__main__":
    unittest.main()
