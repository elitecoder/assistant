"""Regression-pin: assert that no production code path can call
`cmux close-workspace` on a workspace.

After the 2026-05-26 work-loss incident (ws:97 phonebook audit, ws:99 E2E
combining due diligence both auto-closed mid-work), the system removed
all close-workspace capability. This test grep-asserts the rule.

Allowed surfaces:
  - ASCII references in docs / commit messages / INCIDENTS.md / notes.md
    (those describe the historical bug, not invoke the action).
  - Comments explaining why the policy exists.
  - The cleanup.sh script's append_step that writes a "skipped" record.

Forbidden surfaces:
  - Any *.py that actually shells out to `cmux close-workspace`.
  - Any prompt that lists `close-workspace` as an Assistant action.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ASSISTANT = Path(__file__).resolve().parent.parent
REPO_CLEANUP_SKILL = Path.home() / ".claude/skills/cleanup"


def files_with_extension(root: Path, exts: tuple[str, ...]) -> list[Path]:
    out = []
    for ext in exts:
        out.extend(root.rglob(f"*.{ext}"))
    return out


class TestNoCloseWorkspace(unittest.TestCase):

    # The ONE thing we forbid: a code path that actually shells out
    # `cmux close-workspace`. String literals (in ledger records,
    # display whitelists, etc.) are fine — they describe history, not
    # invoke the action.
    FORBIDDEN_INVOCATIONS = [
        re.compile(r'cmux\s+close-workspace\b'),                     # bare command
        re.compile(r'"close-workspace"\s*,?\s*"--workspace"'),        # subprocess argv
        re.compile(r"'close-workspace'\s*,?\s*'--workspace'"),
    ]

    # The single, deliberately-narrow exception (operator-approved 2026-07-06):
    # the comms daemon reaps ITS OWN throwaway warm session. This is NOT the
    # banned case — the 2026-05-26 incident was about automation closing
    # workspaces that hold USER WORK. close_own_workspace() is title-guarded: it
    # closes only an "assistant-comms (warm)" workspace, which user work never
    # is. We allow this ONE exact invocation line and nothing else, so any other
    # close-workspace call (even elsewhere in the same file) still fails.
    ALLOWED_INVOCATION_SUBSTRINGS = {
        'bin/comms_session.py': (
            '[str(paths.cmux_bin), "close-workspace", "--workspace", ws_ref]',
        ),
    }

    def _is_allowed(self, path: Path, line: str) -> bool:
        for rel, allowed in self.ALLOWED_INVOCATION_SUBSTRINGS.items():
            if path.as_posix().endswith(rel):
                return any(a in line for a in allowed)
        return False

    def _scan(self, paths: list[Path], allow_paths: set[Path]) -> list[tuple[Path, int, str, str]]:
        """Returns [(path, line_no, line, pattern)] of forbidden hits."""
        hits = []
        for p in paths:
            if p in allow_paths:
                continue
            try:
                text = p.read_text(errors="replace")
            except Exception:
                continue
            in_docstring = False
            docstring_delim = None
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                # Track multi-line triple-quoted strings so interior lines
                # (e.g. module-level docstrings listing historical commands)
                # are not flagged as actual invocations.
                if not in_docstring:
                    for delim in ('"""', "'''"):
                        if delim in stripped:
                            count = stripped.count(delim)
                            if count % 2 == 1:
                                # Odd number of delimiters → we've entered a
                                # multi-line string that isn't closed on this line.
                                in_docstring = True
                                docstring_delim = delim
                            break
                    # Skip pure single-line comments
                    if stripped.startswith("#") or stripped.startswith("//"):
                        continue
                    # Skip lines that ARE the docstring delimiter (opening or
                    # closing standalone triple-quote lines).
                    if stripped.startswith('"""') or stripped.startswith("'''"):
                        continue
                    for pat in self.FORBIDDEN_INVOCATIONS:
                        if pat.search(line):
                            if self._is_allowed(p, line):
                                continue  # the one title-guarded self-close
                            hits.append((p, line_no, line.rstrip(), pat.pattern))
                else:
                    # Inside a multi-line string — skip but watch for closing.
                    if docstring_delim in stripped:
                        in_docstring = False
                        docstring_delim = None
        return hits

    def test_assistant_repo_has_no_close_workspace_invocation(self):
        py_files = files_with_extension(REPO_ASSISTANT / "bin", ("py", "sh"))
        # Allow this very test file (which discusses the rule) and the docs.
        allow = {Path(__file__).resolve()}
        hits = self._scan(py_files, allow)
        if hits:
            msg = "\n".join(f"  {p.relative_to(REPO_ASSISTANT)}:{n}: [{pat}] {line}" for p, n, line, pat in hits)
            self.fail(f"forbidden close-workspace invocations found in production code:\n{msg}")

    def test_allowlist_is_exact_not_a_file_wide_pass(self):
        """The comms_session.py exception must be the EXACT guarded line, not a
        blanket pass for the file. A different close-workspace invocation in the
        same file must still be flagged."""
        rogue = 'run_cmd([cmux, "close-workspace", "--workspace", some_other_ref])'
        p = Path("/repo/bin/comms_session.py")  # path only used for suffix match
        self.assertFalse(self._is_allowed(p, rogue),
                         "a non-guarded close-workspace line must NOT be allowed")
        # The real guarded line IS allowed.
        guarded = '[str(paths.cmux_bin), "close-workspace", "--workspace", ws_ref]'
        self.assertTrue(self._is_allowed(p, guarded))
        # And the exception does not leak to other files.
        self.assertFalse(self._is_allowed(Path("/repo/bin/pulse.py"), guarded))

    def test_cleanup_skill_does_not_call_close_workspace(self):
        if not REPO_CLEANUP_SKILL.exists():
            self.skipTest("cleanup skill not installed")
        sh_files = list(REPO_CLEANUP_SKILL.glob("*.sh"))
        allow: set[Path] = set()
        hits = self._scan(sh_files, allow)
        if hits:
            msg = "\n".join(f"  {p.name}:{n}: [{pat}] {line}" for p, n, line, pat in hits)
            self.fail(f"cleanup skill scripts must not call close-workspace:\n{msg}")

    def test_no_prompt_lists_close_workspace_action(self):
        """Walk every prompt under prompts/ and assert none of them lists
        close-workspace as an action the system should take. Prompts MAY
        mention it in a 'do not do this' context (those lines contain
        'not'/'never'/'disabled' and are allowed). What's forbidden is
        any markdown table row that treats close-workspace as a verdict
        action."""
        prompts_dir = REPO_ASSISTANT / "prompts"
        if not prompts_dir.exists():
            self.skipTest("no prompts/ dir")
        for prompt in prompts_dir.glob("*.md"):
            text = prompt.read_text()
            for line_no, line in enumerate(text.splitlines(), 1):
                if "close-workspace" not in line.lower():
                    continue
                ll = line.lower()
                if any(neg in ll for neg in ("not", "never", "disabled", "removed", "no longer")):
                    continue
                if line.lstrip().startswith("|") and "|" in line[1:]:
                    self.fail(
                        f"{prompt.name}:{line_no} appears to list close-workspace "
                        f"as an action:\n  {line}"
                    )


if __name__ == "__main__":
    unittest.main()
