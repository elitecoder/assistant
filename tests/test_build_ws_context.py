"""End-to-end CLI-shape tests for bin/build-ws-context.py.

Runs the script as a subprocess (the real entry point the orchestrator calls)
and asserts the OUTPUT CONTRACT. cmux is made unreachable (CMUX_BIN points at a
nonexistent binary) so no live terminal is touched — which means every agent
read fails and `transcript_path` is `null`. That is the deliberate safe
default: with no verifiable signal, we attach NOTHING. The verified-resolution
internals (status-bar id, pane selection, registry gating) are covered by
test_build_ws_context_in_process.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/build-ws-context.py"


def run_ctx_builder(env_home: Path, ws_ref: str, title: str, cwd: str) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(env_home)
    # Make cmux unreachable so the script never touches a live terminal. Every
    # agent-pane read then fails → transcript_path is null (the safe default).
    env["CMUX_BIN"] = "/nonexistent/cmux-for-tests"
    r = subprocess.run(
        ["python3", str(SCRIPT), "--ws-ref", ws_ref, "--title", title, "--cwd", cwd],
        env=env, capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise AssertionError(
            f"build-ws-context failed: rc={r.returncode}\nstderr: {r.stderr}\nstdout: {r.stdout}")
    return json.loads(r.stdout)


def fixture_home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / ".claude/projects").mkdir(parents=True)
    (home / ".factory/sessions").mkdir(parents=True)
    (home / "Library/Application Support/cmux").mkdir(parents=True)
    return home


class TestBuildWsContextCLI(unittest.TestCase):

    def test_payload_shape_and_safe_defaults(self):
        with TemporaryDirectory() as t:
            home = fixture_home(Path(t))
            ctx = run_ctx_builder(home, "workspace:50", "td-999 fake", "/tmp/fake/cwd")
        # Every documented field is present.
        for key in ("ws_ref", "title", "cwd", "transcript_path", "transcript_source",
                    "session_id8", "agent_surface", "last_turn_age_sec", "agent_status",
                    "agent_provider",
                    "cwd_dirty", "cwd_unpushed", "is_protected", "screen_text",
                    "screen_shows_error"):
            self.assertIn(key, ctx, f"missing field: {key}")
        self.assertEqual(ctx["ws_ref"], "workspace:50")
        # cmux unreachable → nothing verified → transcript MUST be null.
        self.assertIsNone(ctx["transcript_path"])
        self.assertIsNone(ctx["transcript_source"])
        self.assertIsNone(ctx["session_id8"])
        self.assertIsNone(ctx["last_turn_age_sec"])
        self.assertEqual(ctx["agent_status"], "idle")
        self.assertEqual(ctx["screen_text"], "")
        self.assertFalse(ctx["screen_shows_error"])

    def test_never_attaches_transcript_without_live_signal(self):
        # THE INVARIANT at the CLI boundary: even if a plausible-looking jsonl
        # exists in the cwd's project dir, with cmux unreachable we must NOT
        # attach it — a wrong transcript is worse than none.
        with TemporaryDirectory() as t:
            home = fixture_home(Path(t))
            proj = home / ".claude/projects/-tmp-fake-cwd"
            proj.mkdir(parents=True)
            decoy = proj / "deadbeef-1111-2222-3333-444444444444.jsonl"
            decoy.write_text(json.dumps({
                "sessionId": "deadbeef-1111-2222-3333-444444444444",
                "message": {"role": "user", "content": "td-999 work"}}) + "\n")
            ctx = run_ctx_builder(home, "workspace:50", "td-999 fake", "/tmp/fake/cwd")
        self.assertIsNone(ctx["transcript_path"],
                          "must not attach a cwd jsonl without a verified live signal")

    def test_no_pr_data_field_in_output(self):
        # Regression: PR-related fields must NEVER appear (Observer fetches on demand).
        with TemporaryDirectory() as t:
            home = fixture_home(Path(t))
            ctx = run_ctx_builder(home, "workspace:50", "td-999", "/tmp/fake/cwd")
        forbidden = {"pr_data", "pr_refs", "prior_summary",
                     "prior_classification", "prior_proposed_actions",
                     "assistant_policies_excerpt"}
        self.assertEqual(forbidden & set(ctx.keys()), set())

    def test_cwd_state_reflects_real_repo(self):
        with TemporaryDirectory() as t:
            home = fixture_home(Path(t))
            repo = Path(t) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            (repo / "f.txt").write_text("hello")  # dirty, untracked
            ctx = run_ctx_builder(home, "workspace:50", "any", str(repo))
        self.assertTrue(ctx["cwd_dirty"])

    def test_protected_workspace_flag(self):
        with TemporaryDirectory() as t:
            home = fixture_home(Path(t))
            ctx_p = run_ctx_builder(home, "workspace:3", "any", "/tmp/x")
            ctx_n = run_ctx_builder(home, "workspace:50", "any", "/tmp/x")
        self.assertTrue(ctx_p["is_protected"], "workspace:3 must be protected")
        self.assertFalse(ctx_n["is_protected"])


if __name__ == "__main__":
    unittest.main()
