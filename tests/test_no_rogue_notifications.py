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

Two scans run per file (F17 — a bare per-line literal grep is trivially
evaded):
  1. a CASE-INSENSITIVE text scan (catches `OSASCRIPT`, `Osascript`, …);
  2. an AST scan that CONSTANT-FOLDS string expressions before searching, so
     split-literal / concatenation dodges are caught:
       "osas" + "cript"          (BinOp folding)
       ["osa" "script"]          (implicit adjacent-literal concat)
       "".join(["osa","script"]) (constant str.join)
       f"osa{''}script"          (f-string constant parts)
Known residual gap (documented, not closed): fully DYNAMIC construction at
runtime — getattr on a module, a binary read from a file, bytes.decode of an
obfuscated blob — still evades a static scan. The AST fold catches the
literal-splitting a human reaches for first; defeating it now takes
deliberate obfuscation that would not survive review. The exact-path gate
exemption below is the one intentional hole.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("bin", "src")
# The one permitted push call site.
ALLOWLIST = {Path("bin") / "interrupt-gate.py"}

# Case-insensitive so `OSASCRIPT` / `Chat.PostMessage` can't slip past. The
# Slack send is matched in BOTH its Web-API (`chat.postMessage`) and Python-SDK
# (`chat_postMessage`) spellings via `chat[._]post_?message`.
PUSH_PATTERN = re.compile(
    r"osascript|display notification|terminal-notifier|"
    r"chat[._]post_?message",
    re.IGNORECASE)


def _fold_str(node):
    """Best-effort constant fold of a string-valued AST node → its str value,
    or None when it isn't a pure constant expression. Handles the literal
    dodges a human reaches for: `a + b`, implicit adjacent concat (already
    merged by the parser into one Constant), `"sep".join([...])`, f-strings
    with constant parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_str(node.left)
        right = _fold_str(node.right)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.JoinedStr):  # f-string
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
        return "".join(parts)
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "join":
            sep = _fold_str(fn.value)
            if sep is not None and node.args and isinstance(
                    node.args[0], (ast.List, ast.Tuple)):
                elts = [_fold_str(e) for e in node.args[0].elts]
                if all(e is not None for e in elts):
                    return sep.join(elts)
    return None


def scan_source(text: str) -> list[str]:
    """Every distinct forbidden token this source reveals, via the
    case-insensitive text scan AND the constant-folding AST scan. Empty list
    means clean. Exposed at module scope so the decoy test can exercise it
    without planting rogue files under bin/src."""
    hits: list[str] = []
    for m in PUSH_PATTERN.finditer(text):
        hits.append(m.group(0).lower())
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # A file using syntax this interpreter can't parse (e.g. match/case
        # under 3.9) still got the text scan above — don't crash the guard.
        return sorted(set(hits))
    for node in ast.walk(tree):
        folded = _fold_str(node)
        if folded and PUSH_PATTERN.search(folded):
            hits.append(PUSH_PATTERN.search(folded).group(0).lower())
    return sorted(set(hits))


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
                # Line scan (keeps the precise file:line report) …
                for n, line in enumerate(text.splitlines(), 1):
                    if PUSH_PATTERN.search(line):
                        offenders.append(f"{rel}:{n}: {line.strip()[:120]}")
                # … plus the constant-folding scan for split-literal dodges
                # that no single line reveals.
                if path.suffix == ".py":
                    folded_hits = set(scan_source(text))
                    line_hits = {PUSH_PATTERN.search(l).group(0).lower()
                                 for l in text.splitlines()
                                 if PUSH_PATTERN.search(l)}
                    for extra in sorted(folded_hits - line_hits):
                        offenders.append(
                            f"{rel}: split-literal push token {extra!r} "
                            "(constant-folded)")
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

    def test_split_token_and_concat_decoys_are_caught(self):
        """The dodges a per-line literal grep misses (F17): mixed case, string
        concatenation, implicit adjacent-literal concat, constant join, and
        f-string assembly of the forbidden binaries."""
        decoys = [
            'x = "OSASCRIPT"',                              # case
            'x = "osas" + "cript"',                         # + concat
            'subprocess.run(["osa" "script", "-e", s])',    # implicit concat
            'cmd = "".join(["osa", "script"])',             # constant join
            'name = f"osa{\'\'}script"',                    # f-string parts
            'y = "terminal-" + "notifier"',                 # + concat, 2nd bin
            'z = "chat." + "postMessage"',                  # + concat, Slack
        ]
        for src in decoys:
            self.assertTrue(
                scan_source(src),
                f"decoy evaded the scan: {src!r}")

    def test_clean_source_is_not_flagged(self):
        """No false positives on ordinary code that merely mentions unrelated
        words."""
        clean = [
            'x = "join the queue"',
            'msg = "display the results"',
            'run(["git", "status"])',
        ]
        for src in clean:
            self.assertEqual(scan_source(src), [], f"false positive: {src!r}")


# ── Outbound-send chokepoint (Keel M7.d) ────────────────────────────────────
# The outbound counterpart of the push guard: the primitives that actually send
# to the external world may appear ONLY inside the gated outbound path.
# Everything M7 does to the outside is a DRAFT (Gmail drafts.create) or staged
# paste-text — never a send. Covered here: SMTP (smtplib/sendmail), a PR merge
# (`gh pr merge`, including the argv-split `["gh","pr","merge"]` form via the
# list-fold below), and the Gmail send surfaces (messages().send/drafts().send).
# The Slack SEND spellings (chat.postMessage / chat_postMessage) are covered by
# the PUSH guard above. NOTE: this is DEFENSE-IN-DEPTH, not the primary barrier
# (the two-gate dispatcher is) — a determined dynamic-construction dodge
# (getattr, `gh api … PUT …/merge`, an arbitrary webhook POST) is out of scope;
# the point is that no ORDINARY send primitive lands outside the gate by accident.
OUTBOUND_PATTERN = re.compile(
    r"smtplib|sendmail|gh\s+pr\s+merge|messages\(\)\.send|drafts\(\)\.send",
    re.IGNORECASE)
# The only files permitted to carry a real send primitive. All three are the
# sanctioned executors; none actually contains one today (this guard is a
# forward fence), but a future send-capable line must live in one of them.
OUTBOUND_ALLOWLIST = {
    Path("bin") / "outbound-dispatch.py",
    Path("bin") / "merge-pr-dispatch.py",
    Path("bin") / "cmux-send.py",
}


def scan_outbound(text: str) -> list[str]:
    """Every distinct forbidden outbound token in `text`, via the text scan, the
    constant-folding scan (`"gh pr " + "merge"`), AND an argv-list fold that
    joins a list/tuple of string constants with a space — the natural
    `subprocess.run(["gh", "pr", "merge", n])` form a plain grep misses."""
    hits: list[str] = []
    for m in OUTBOUND_PATTERN.finditer(text):
        hits.append(m.group(0).lower())
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return sorted(set(hits))
    for node in ast.walk(tree):
        folded = _fold_str(node)
        if folded and OUTBOUND_PATTERN.search(folded):
            hits.append(OUTBOUND_PATTERN.search(folded).group(0).lower())
        # argv-list: join the string-constant elements with a space so a
        # command split across list elements is scanned as one command line.
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(parts) >= 2:
                joined = " ".join(parts)
                m = OUTBOUND_PATTERN.search(joined)
                if m:
                    hits.append(m.group(0).lower())
    return sorted(set(hits))


class NoRogueOutboundTest(unittest.TestCase):
    def test_no_send_primitives_outside_the_gate(self):
        offenders = []
        for d in SCAN_DIRS:
            root = REPO / d
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(REPO)
                if rel in OUTBOUND_ALLOWLIST:
                    continue
                if path.suffix in (".pyc",):
                    continue
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if OUTBOUND_PATTERN.search(line):
                        offenders.append(f"{rel}:{n}: {line.strip()[:120]}")
                if path.suffix == ".py":
                    folded_hits = set(scan_outbound(text))
                    line_hits = {OUTBOUND_PATTERN.search(l).group(0).lower()
                                 for l in text.splitlines()
                                 if OUTBOUND_PATTERN.search(l)}
                    for extra in sorted(folded_hits - line_hits):
                        offenders.append(
                            f"{rel}: split-literal outbound token {extra!r} "
                            "(constant-folded)")
        self.assertEqual(
            offenders, [],
            "outbound send primitives (SMTP / `gh pr merge`) outside the gated "
            "executors (route them through bin/outbound-dispatch.py — Keel M7):\n"
            + "\n".join(offenders))

    def test_outbound_allowlist_files_exist(self):
        # A renamed executor would silently widen the fence — pin their presence.
        for rel in OUTBOUND_ALLOWLIST:
            self.assertTrue((REPO / rel).is_file(), f"{rel} missing")

    def test_outbound_decoys_are_caught(self):
        decoys = [
            'import smtplib',
            's = "smtp" + "lib"',                        # + concat
            'cmd = "gh pr " "merge"',                    # implicit adjacent concat
            'cmd = "gh pr " + "merge"',                  # + concat
            'cmd = "".join(["gh pr ", "merge"])',        # constant join
            'subprocess.run(["gh", "pr", "merge", n])',  # argv-split
            'run(["/usr/sbin/sendmail", "-t"], input=b)',  # sendmail argv
            'service.users().messages().send(userId=u)',  # gmail send
        ]
        for src in decoys:
            self.assertTrue(scan_outbound(src), f"decoy evaded: {src!r}")

    def test_outbound_clean_source_not_flagged(self):
        clean = [
            'run(["gh", "pr", "view", pr])',   # view is fine
            'x = "merge the branch"',
            'note = "send it to the queue"',
        ]
        for src in clean:
            self.assertEqual(scan_outbound(src), [], f"false positive: {src!r}")


if __name__ == "__main__":
    unittest.main()
