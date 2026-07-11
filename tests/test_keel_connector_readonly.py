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
# The Node slack-reactor holds the send-capable @slack/web-api WebClient; a
# future chat.postMessage there must fail CI (finding 12a) even though it is JS,
# not Python.
SLACK_REACTOR_SRC = REPO / "slack-reactor" / "src"

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
# `gh api` with a SPACE-separated method override (`-X POST`, `--method post`) —
# the glued `-XPOST` form is covered by _XVERB_FLAGS; these catch the split form.
_GH_SPACE_METHOD = tuple(
    f"{flag} {verb}"
    for flag in ("-x", "--method")
    for verb in _WRITE_VERBS)
# Send/mutation API names, matched across folded concatenation, case-normalized.
_SEND_TOKENS = ("chat.postmessage", "messages.send", "drafts.send",
                "users.messages.send", ".send_message")
# Slack Web API message-SEND methods forbidden in the Node reactor (finding 12a).
# A reaction (reactions.add) and reads (conversations.*, chat.getpermalink) are
# allowed; only the send family is not.
_NODE_SEND_TOKENS = ("chat.postmessage", "chat.postephemeral", "chat.update",
                     "chat.memessage", "chat.schedulemessage",
                     "chat.deletescheduledmessage", ".postmessage(")


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
        # SEC1 follow-up (finding 12b): `gh api -X POST` / `--method post` with a
        # SPACE, which the glued-verb check above misses.
        if "gh api" in low and any(m in low for m in _GH_SPACE_METHOD):
            findings.append(f"gh api space-form write method in {s!r}")
        # Implicit POST via `curl -d` / `curl --data…` (finding 12b).
        if "curl" in low and re.search(r"(?:^|\s)(?:-d|--data\b|--data-[a-z]+)",
                                       low):
            findings.append(f"curl implicit POST (data) in {s!r}")
    # List-form `["gh","api","-XPOST",…]` — tokens are separate literals.
    if "gh" in atomic and "api" in atomic and (
            atomic & {"-f", "--field", "--input", "--method", "--raw-field"}
            or any(a.startswith("-x") for a in atomic)
            or _WRITE_VERBS & atomic):
        findings.append("gh api invoked with a write flag/verb (list form)")
    # Implicit POST via urllib: a Request/urlopen call carrying a non-None
    # `data=` keyword IS a POST even with no method= (finding 12b). The OAuth
    # token POST lives in the BASE (connector.py), which this scanner is never
    # run against — so a connector script that adds a urllib data= call is a real
    # violation.
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fname = _dotted_name(n.func).lower()
        if not ("request" in fname or "urlopen" in fname):
            continue
        for kw in n.keywords:
            if kw.arg == "data" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is None):
                findings.append(f"urllib implicit POST (data=) via {fname!r}")
    return findings


def _dotted_name(node) -> str:
    """The dotted attribute path of a call target, e.g. urllib.request.urlopen."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def scan_node_source(text: str) -> list:
    """Read-only violations in a Node reactor source (finding 12a). JS is not
    AST-parsed here; a case-normalized, comment-and-concatenation-tolerant scan
    for the Slack message-SEND family is enough to fail CI the moment a
    chat.postMessage (or sibling send) appears. Folds `+`-concatenated string
    literals so `"chat.post"+"Message"` cannot hide the token."""
    findings = []
    # Strip comments first so PROSE that names chat.postMessage (the reactor's
    # own "never chat.postMessage" note) is not a false positive — only real code
    # is scanned. Then fold `+`-concatenated string literals so a split token
    # (`"chat.post"+"Message"`) cannot hide.
    no_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    no_comments = re.sub(r"//[^\n]*", "", no_comments)
    folded = re.sub(r"['\"]\s*\+\s*['\"]", "", no_comments).lower()
    for tok in _NODE_SEND_TOKENS:
        if tok in folded:
            findings.append(f"node send-API token {tok!r}")
    return findings


def _node_scripts():
    if not SLACK_REACTOR_SRC.is_dir():
        return []
    return sorted(p for p in SLACK_REACTOR_SRC.glob("*.js"))


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

    # ── finding 12b: space-form gh api, curl -d, urllib data= implicit POST ──

    def test_scan_catches_space_form_and_implicit_post_decoys(self):
        decoys = {
            "gh api -X POST (space, string)":
                'import subprocess\n'
                'subprocess.run("gh api -X POST /repos/x/y/issues/1/comments")\n',
            "gh api --method post (space)":
                'import subprocess\n'
                'subprocess.run("gh api --method post /repos/x/y/merges")\n',
            "curl -d implicit POST":
                'import subprocess\n'
                'subprocess.run("curl -d body=hi https://api/x")\n',
            "curl --data-raw implicit POST":
                'import subprocess\n'
                'subprocess.run("curl --data-raw @f https://api/x")\n',
            "urllib data= implicit POST":
                'import urllib.request\n'
                'urllib.request.urlopen(url, data=body)\n',
            "urllib Request(data=) implicit POST":
                'import urllib.request\n'
                'req = urllib.request.Request(url, data=payload)\n',
        }
        for label, src in decoys.items():
            self.assertNotEqual(
                scan_connector_source(src), [],
                msg=f"decoy NOT caught: {label}")

    def test_readonly_urllib_and_get_gh_stay_clean(self):
        # A GET with data=None and a read `gh api` (no write flag) must NOT flag.
        clean = (
            'import urllib.request\n'
            'urllib.request.urlopen(url)\n'
            'http("GET", url, data=None)\n'
            'connector.urllib.parse.quote(jql)\n'
            'subprocess.run("gh api /repos/x/y")\n')
        self.assertEqual(scan_connector_source(clean), [])

    # ── finding 12a: the Node reactor is scanned for chat.postMessage ────────

    def test_node_reactor_scripts_exist(self):
        self.assertTrue(_node_scripts(),
                        "no Node reactor scripts found under slack-reactor/src/")

    def test_node_reactor_has_no_send_calls(self):
        for f in _node_scripts():
            self.assertEqual(
                scan_node_source(f.read_text()), [],
                msg=f"{f.name} contains a Slack send call")

    def test_node_scan_catches_send_decoys(self):
        decoys = {
            "chat.postMessage":
                "await bot.chat.postMessage({ channel, text: 'hi' })\n",
            "postMessage shorthand":
                "await web.postMessage({channel})\n",
            "concat chat.post+Message":
                "const m = 'chat.post' + 'Message'\n",
            "chat.update":
                "await bot.chat.update({ channel, ts })\n",
        }
        for label, src in decoys.items():
            self.assertNotEqual(
                scan_node_source(src), [],
                msg=f"node decoy NOT caught: {label}")


if __name__ == "__main__":
    unittest.main()
