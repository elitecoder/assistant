"""CI guard: connectors are READ-ONLY producers (design section 9).

A connector's only job is source→WorldEvent→inbox; it must never call a
mutation/send API. This grep test fails if any connector script (or the base)
contains a GitHub/Slack/mail mutation call, and asserts the connector scripts
issue only GET requests (the base's single POST is the OAuth token endpoint,
which is auth, not a content mutation — allowed only in connector.py).

The naive substring/exact-literal grep (M3) was evadable (SEC1): `gh api
-XPOST` mutates via the API without ever naming `gh pr merge`; a concatenated
verb `method="po""st"` or `"chat.post"+"Message"` hides the token from a flat
substring scan; and a lowercase verb dodges an exact-`"POST"` check. The
strengthened scanner below FOLDS string-literal concatenations (adjacency AND
`+`) via the AST, normalizes case, flags `gh api` with any write method/`-f`/
`--field`/`-X`, and detects the send-API tokens across concatenation.

New module (sorts after test_daemon), unittest style.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONNECTORS_DIR = REPO / "bin" / "connectors"
BASE = REPO / "src" / "assistant" / "connector.py"

# Mutation / send call-sites no connector may ever contain (identifier forms).
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

# ── strengthened AST/tokenizer scan (SEC1) ───────────────────────────────────

_WRITE_VERBS = frozenset({"post", "put", "patch", "delete"})
_XVERB_FLAGS = ("-xpost", "-xput", "-xpatch", "-xdelete")
_WRITE_FLAGS = ("-f", "--field", "--input", "--method", "--raw-field")
# Send/mutation API names, matched across folded concatenation, case-normalized.
_SEND_TOKENS = ("chat.postmessage", "messages.send", "drafts.send",
                "users.messages.send", ".send_message")


def _fold(node):
    """The string a node evaluates to when it is a (possibly concatenated)
    string literal — folding adjacency (already merged by the parser), `+`
    BinOps, and the literal parts of f-strings. None for anything else. This is
    what defeats `"chat.post"+"Message"` and `method="po""st"`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            parts.append(v.value if isinstance(v, ast.Constant)
                         and isinstance(v.value, str) else "")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def scan_connector_source(text: str) -> list:
    """Return a list of read-only violations found in `text`. Empty == clean.
    Used both to prove the shipped connectors are clean AND to prove the M3
    decoys are caught."""
    findings = []
    for pat in FORBIDDEN:
        if re.search(pat, text):
            findings.append(f"forbidden identifier: {pat!r}")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return findings
    folded = {f for f in (_fold(n) for n in ast.walk(tree)) if f is not None}
    atomic = {n.value.lower() for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for s in folded:
        low = s.lower()
        for tok in _SEND_TOKENS:
            if tok in low:
                findings.append(f"send-API token {tok!r} in {s!r}")
        for xv in _XVERB_FLAGS:
            if xv in low:
                findings.append(f"gh api write verb {xv!r} in {s!r}")
        if low.strip() in _WRITE_VERBS:
            findings.append(f"HTTP write-verb literal {s!r}")
        if "gh api" in low and any(fl in low for fl in _WRITE_FLAGS):
            findings.append(f"gh api write flag in {s!r}")
    # List-form `["gh","api","-XPOST",…]` — tokens are separate literals.
    if "gh" in atomic and "api" in atomic and (
            atomic & {"-f", "--field", "--input", "--method", "--raw-field"}
            or any(a.startswith("-x") for a in atomic)
            or _WRITE_VERBS & atomic):
        findings.append("gh api invoked with a write flag/verb (list form)")
    return findings


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

    # ── SEC1: strengthened scan ──────────────────────────────────────────────

    def test_strengthened_scan_is_clean_on_shipped_connectors(self):
        for f in _connector_scripts():
            self.assertEqual(
                scan_connector_source(f.read_text()), [],
                msg=f"{f.name} flagged by the read-only scanner")

    def test_scan_catches_m3_style_decoys(self):
        decoys = {
            "gh api -XPOST (string)":
                'import subprocess\n'
                'subprocess.run("gh api -XPOST /repos/x/y/issues/1/comments")\n',
            "gh api -XPOST (list)":
                'import subprocess\n'
                'subprocess.run(["gh","api","-XPOST","/repos/x/y/merges"])\n',
            "gh api -f write (list)":
                'import subprocess\n'
                'subprocess.run(["gh","api","-f","body=hi","/repos/x/y"])\n',
            'concat method="po""st"':
                'http("GET", url, method="po""st")\n',
            'concat verb "po"+"st"':
                'verb = "po" + "st"\n',
            'split chat.post+Message':
                'api = "chat.post" + "Message"\n',
            "lowercase gh pr merge":
                'cmd = "gh pr merge 5"\n',
        }
        for label, src in decoys.items():
            self.assertNotEqual(
                scan_connector_source(src), [],
                msg=f"decoy NOT caught: {label}")


if __name__ == "__main__":
    unittest.main()
