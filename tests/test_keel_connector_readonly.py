"""CI guard: connectors are READ-ONLY producers (design section 9).

A connector's only job is source→WorldEvent→inbox; it must never call a
mutation/send API. This grep test fails if any connector script (or the base)
contains a GitHub/Slack/mail mutation call, and asserts the connector scripts
issue only GET requests (the base's single POST is the OAuth token endpoint,
which is auth, not a content mutation — allowed only in connector.py).

New module (sorts after test_daemon), unittest style.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONNECTORS_DIR = REPO / "bin" / "connectors"
BASE = REPO / "src" / "assistant" / "connector.py"

# Mutation / send call-sites no connector may ever contain.
FORBIDDEN = [
    r"gh\s+pr\s+merge",
    r"gh\s+pr\s+close",
    r"chat\.postMessage",
    r"\bsmtplib\b",
    r"\bsendmail\b",
    r"email\.send",
    r"\.send_message\(",
    r"messages\.send",       # gmail send
    r"users\.messages\.send",
    r"drafts\.send",
]


def _connector_scripts():
    return sorted(p for p in CONNECTORS_DIR.glob("*.py"))


class ReadOnlyTests(unittest.TestCase):
    def test_connector_scripts_exist(self):
        self.assertTrue(_connector_scripts(),
                        "no connector scripts found under bin/connectors/")

    def test_no_mutation_or_send_calls_in_connectors(self):
        targets = _connector_scripts() + [BASE]
        for f in targets:
            text = f.read_text()
            for pat in FORBIDDEN:
                self.assertIsNone(
                    re.search(pat, text),
                    msg=f"{f.name} contains a forbidden mutation/send: {pat!r}")

    def test_connector_scripts_issue_only_GET(self):
        # The actual connectors (bin/connectors/*.py) must never name a
        # mutating HTTP verb — they read, they never write to the source.
        for f in _connector_scripts():
            text = f.read_text()
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertNotIn(
                    f'"{verb}"', text,
                    msg=f"{f.name} names a mutating HTTP verb {verb}")

    def test_base_only_posts_to_the_oauth_token_endpoint(self):
        # connector.py DOES POST — but only for the OAuth refresh grant. Prove
        # the sole POST call-site is the token transport, nothing else.
        text = BASE.read_text()
        post_lines = [ln for ln in text.splitlines()
                      if 'method="POST"' in ln or "method='POST'" in ln]
        # exactly one POST call-site, and it targets the OAuth token endpoint
        self.assertEqual(len(post_lines), 1, msg=str(post_lines))
        self.assertIn("token", post_lines[0].lower())


if __name__ == "__main__":
    unittest.main()
