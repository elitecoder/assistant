"""CI chokepoint test (Keel M3, design section 8): bin/interrupt-gate.py is
the ONLY module in bin/ + src/ permitted to touch a push surface. This test
greps every file under those trees for push-API markers and fails on any
hit outside the gate — a new notification call site cannot merge without
either routing through the gate or consciously editing this allowlist (and
answering for it in review).

The pattern list covers the surfaces the design names: osascript (macOS
Notification Center), `display notification` (the AppleScript verb, in case
someone shells it differently), terminal-notifier, and Slack's
chat.postMessage. Docstrings count as hits on purpose — the cheapest way to
keep the forbidden strings out of copy-pasteable reach is to keep them out
of the trees entirely.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("bin", "src")
# The one permitted push call site.
ALLOWLIST = {Path("bin") / "interrupt-gate.py"}

PUSH_PATTERN = re.compile(
    r"osascript|display notification|terminal-notifier|chat\.postMessage")


class NoRogueNotificationsTest(unittest.TestCase):
    def test_no_push_calls_outside_the_gate(self):
        offenders = []
        for d in SCAN_DIRS:
            root = REPO / d
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(REPO)
                if rel in ALLOWLIST:
                    continue
                if path.suffix in (".pyc",):
                    continue
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if PUSH_PATTERN.search(line):
                        offenders.append(f"{rel}:{n}: {line.strip()[:120]}")
        self.assertEqual(
            offenders, [],
            "push-surface call sites outside bin/interrupt-gate.py "
            "(route them through the gate — design section 8):\n"
            + "\n".join(offenders))

    def test_the_gate_itself_is_present(self):
        """The allowlist must point at a real file — a renamed gate would
        otherwise pass the grep while breaking every caller."""
        for rel in ALLOWLIST:
            self.assertTrue((REPO / rel).is_file(), f"{rel} missing")


if __name__ == "__main__":
    unittest.main()
