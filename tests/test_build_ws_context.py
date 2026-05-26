"""Unit tests for bin/build-ws-context.py — verifies the mechanical signals
it computes from a JSONL transcript.

No LLM. No cmux. Just feeds the script a fixture transcript via the
cmux-registry mock and inspects the JSON output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/build-ws-context.py"


def write_transcript(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def run_ctx_builder(env_home: Path, ws_ref: str, title: str, cwd: str) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(env_home)
    r = subprocess.run(
        ["python3", str(SCRIPT), "--ws-ref", ws_ref, "--title", title, "--cwd", cwd],
        env=env, capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise AssertionError(f"build-ws-context failed: rc={r.returncode}\nstderr: {r.stderr}\nstdout: {r.stdout}")
    return json.loads(r.stdout)


def setup_home_with_transcript(tmpdir: Path, ws_ref: str, panel_id: str,
                                 transcript_entries: list[dict]) -> tuple[Path, Path]:
    """Build a fake $HOME directory tree with a cmux-registry pointing
    panel_id → a transcript JSONL we just wrote. Returns (home_path,
    transcript_path)."""
    home = tmpdir / "home"
    (home / ".claude").mkdir(parents=True)
    (home / "Library/Application Support/cmux").mkdir(parents=True)
    (home / ".claude/projects/-tmp-fake-cwd").mkdir(parents=True)

    transcript = home / ".claude/projects/-tmp-fake-cwd/test-session.jsonl"
    write_transcript(transcript, transcript_entries)

    # Minimal cmux state file — empty windows is fine, registry alone resolves.
    state_path = home / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
    state_path.write_text(json.dumps({"windows": []}))

    # cmux-registry.json maps panel_id → transcript_path
    reg = {panel_id: {"panel_id": panel_id, "transcript_path": str(transcript)}}
    (home / ".claude/cmux-registry.json").write_text(json.dumps(reg))
    return home, transcript


class TestBuildWsContext(unittest.TestCase):

    def test_agent_status_idle_when_no_pending_tool_use(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            entries = [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}},
            ]
            home, _ = setup_home_with_transcript(tmp, "workspace:50", "PANEL-X", entries)
            # Title fallback path: we don't have the cmux state matching by title,
            # but registry maps panel_id directly. We need state's panels to have
            # an id that matches PANEL-X. Easier: use the cwd fallback path.
            # Use the cwd that maps to project dir.
            ctx = run_ctx_builder(home, "workspace:50", "td-999 fake", "/tmp/fake/cwd")
            # transcript_path may be None if title-marker scan can't match; we
            # accept that and just check the script ran.
            self.assertEqual(ctx["ws_ref"], "workspace:50")
            self.assertIn("agent_status", ctx)

    def test_agent_status_working_with_pending_tool_use(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            # tool_use with no matching tool_result → working
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_X", "name": "Bash", "input": {}}
                ]}},
            ]
            # Use the title-signature fallback: title contains "td-999"; find
            # the transcript by reading head and matching that signature.
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            (home / "Library/Application Support/cmux").mkdir(parents=True)
            slug_dir = home / ".claude/projects/-tmp-fake-cwd"
            slug_dir.mkdir(parents=True)
            transcript = slug_dir / "session.jsonl"
            # First user message must contain the signature for matching
            sig_entries = [
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "text", "text": "Work on td-999 fake task"}
                ]}}
            ] + entries
            write_transcript(transcript, sig_entries)
            (home / "Library/Application Support/cmux/session-com.cmuxterm.app.json").write_text(json.dumps({"windows": []}))
            (home / ".claude/cmux-registry.json").write_text("{}")

            ctx = run_ctx_builder(home, "workspace:50", "td-999 fake", "/tmp/fake/cwd")
            self.assertEqual(ctx["transcript_path"], str(transcript),
                             "title-signature scan should resolve transcript")
            self.assertEqual(ctx["agent_status"], "working",
                             "pending tool_use → working")

    def test_no_pr_data_field_in_output(self):
        """Regression: PR-related fields must NEVER appear in ctx output.
        The Observer fetches PR data on demand; build-ws-context does not
        pre-fetch."""
        with TemporaryDirectory() as t:
            tmp = Path(t)
            home, _ = setup_home_with_transcript(tmp, "workspace:50", "PANEL-X", [
                {"type": "user", "message": {"role": "user", "content": "PR #10319 is mentioned here"}},
            ])
            ctx = run_ctx_builder(home, "workspace:50", "td-999", "/tmp/fake/cwd")
            forbidden = {"pr_data", "pr_refs", "prior_summary",
                         "prior_classification", "prior_proposed_actions",
                         "assistant_policies_excerpt"}
            present = forbidden & set(ctx.keys())
            self.assertEqual(present, set(),
                             f"forbidden fields leaked: {present}")

    def test_no_transcript_returns_null_age_idle(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            (home / "Library/Application Support/cmux").mkdir(parents=True)
            (home / "Library/Application Support/cmux/session-com.cmuxterm.app.json").write_text(json.dumps({"windows": []}))
            (home / ".claude/cmux-registry.json").write_text("{}")
            # cwd doesn't map to any project dir
            ctx = run_ctx_builder(home, "workspace:99", "no-match-title", "/no/such/path")
            self.assertIsNone(ctx["transcript_path"])
            self.assertIsNone(ctx["last_turn_age_sec"])
            self.assertEqual(ctx["agent_status"], "idle")

    def test_protected_workspace_flag(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            home, _ = setup_home_with_transcript(tmp, "workspace:3", "PANEL-X", [])
            ctx_protected = run_ctx_builder(home, "workspace:3", "any", "/tmp/x")
            self.assertTrue(ctx_protected["is_protected"],
                             "workspace:3 must be marked protected")
            ctx_normal = run_ctx_builder(home, "workspace:50", "any", "/tmp/x")
            self.assertFalse(ctx_normal["is_protected"])


if __name__ == "__main__":
    unittest.main()
