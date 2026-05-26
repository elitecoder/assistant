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
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                # Skip pure comments/docstrings
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                for pat in self.FORBIDDEN_INVOCATIONS:
                    if pat.search(line):
                        hits.append((p, line_no, line.rstrip(), pat.pattern))
        return hits

    def test_assistant_repo_has_no_close_workspace_invocation(self):
        py_files = files_with_extension(REPO_ASSISTANT / "bin", ("py", "sh"))
        # Allow this very test file (which discusses the rule) and the docs.
        allow = {Path(__file__).resolve()}
        hits = self._scan(py_files, allow)
        if hits:
            msg = "\n".join(f"  {p.relative_to(REPO_ASSISTANT)}:{n}: [{pat}] {line}" for p, n, line, pat in hits)
            self.fail(f"forbidden close-workspace invocations found in production code:\n{msg}")

    def test_cleanup_skill_does_not_call_close_workspace(self):
        if not REPO_CLEANUP_SKILL.exists():
            self.skipTest("cleanup skill not installed")
        sh_files = list(REPO_CLEANUP_SKILL.glob("*.sh"))
        allow: set[Path] = set()
        hits = self._scan(sh_files, allow)
        if hits:
            msg = "\n".join(f"  {p.name}:{n}: [{pat}] {line}" for p, n, line, pat in hits)
            self.fail(f"cleanup skill scripts must not call close-workspace:\n{msg}")

    def test_assistant_prompt_does_not_list_close_workspace_action(self):
        prompt = REPO_ASSISTANT / "prompts/prompt-assistant-agent.md"
        text = prompt.read_text()
        # The prompt may MENTION close-workspace in a "do not do this" note.
        # What we forbid is any TABLE ROW or RULE that treats close-workspace
        # as an action the Assistant should take. Approximate: forbid the
        # exact string `close-workspace` appearing in a markdown table row
        # cell that doesn't also contain "not", "never", or "DISABLED".
        for line_no, line in enumerate(text.splitlines(), 1):
            if "close-workspace" not in line.lower():
                continue
            ll = line.lower()
            if any(neg in ll for neg in ("not", "never", "disabled", "removed", "no longer")):
                continue
            # Plain mention in narrative is OK if it explains; assert the
            # line isn't an action-table row.
            if line.lstrip().startswith("|") and "|" in line[1:]:
                self.fail(f"prompt-assistant-agent.md:{line_no} appears to list close-workspace in an action table:\n  {line}")


if __name__ == "__main__":
    unittest.main()
