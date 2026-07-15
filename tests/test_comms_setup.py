"""Hermetic onboarding test for bin/assistant-comms-setup.sh — the first-run
Slack-comms setup script a new engineer runs.

The script had ZERO automated coverage (validated only by hand). It is the exact
path a fresh install hits, and its most fragile part — the embedded Python
config-write heredoc that encodes the SEND-GATE invariant (allowed_targets ==
[target]) — must not silently break.

Fully hermetic, no network, no real Slack, no production change:
  - The script derives REPO_DIR from its own location and invokes
    $REPO_DIR/bin/{slack-send.py,assistant-doctor.py} BY PATH. So we copy the
    REAL script verbatim into a throwaway repo whose bin/ holds STUBS for those
    two — REPO_DIR redirects to the stubs.
  - `curl` (auth.test) is invoked via PATH lookup, so a stub curl on PATH
    controls the auth response.
  - $HOME is a tmp dir; config.json + logs land there. Real python3 runs the
    config heredoc (that's the logic under test — we don't stub it).

Each stub records its invocation and honors an env-controlled exit code, so a
test can drive the happy path AND every failure branch (missing token, auth
failure, send failure, doctor failure).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "bin" / "assistant-comms-setup.sh"

OK_AUTH = '{"ok":true,"user_id":"U0BOT","team":"TestTeam"}'
FAIL_AUTH = '{"ok":false,"error":"invalid_auth"}'

_CURL_STUB = """#!/bin/sh
# Stub curl: ignore args, emit the controlled auth.test JSON. The real script
# only calls curl for https://slack.com/api/auth.test.
printf '%s' "${FAKE_AUTH_JSON}"
"""

# slack-send.py / assistant-doctor.py stubs: invoked as `python3 <path> ...args`,
# so they need no +x bit. They log argv and exit with an env-controlled code.
_SEND_STUB = """import json, os, sys
with open(os.environ["SEND_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"stub": "slack-send", "argv": sys.argv[1:]}))
sys.exit(int(os.environ.get("SEND_EXIT", "0")))
"""

_DOCTOR_STUB = """import os, sys
with open(os.environ["DOCTOR_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("DOCTOR_EXIT", "0")))
"""


@unittest.skipUnless(shutil.which("zsh"), "zsh required to run the setup script")
class CommsSetupTests(unittest.TestCase):
    def _run(self, *, token="xoxb-test-fake", ping_target="C0TEST123",
             auth_json=OK_AUTH, send_exit=0, doctor_exit=0,
             seed_config=None):
        """Run the REAL setup script in a fake repo (stubbed bin/) + isolated
        $HOME. Returns (proc, home_path, send_log_lines, doctor_log_lines)."""
        self._td = TemporaryDirectory()
        root = Path(self._td.name)
        home = root / "home"
        fake_repo = root / "repo"
        stubbin = root / "stubbin"
        for d in (home / ".assistant", fake_repo / "bin", stubbin):
            d.mkdir(parents=True)

        # Copy the REAL script verbatim → REPO_DIR resolves to fake_repo, so it
        # invokes our stub slack-send.py / assistant-doctor.py.
        shutil.copy2(SETUP, fake_repo / "bin" / "assistant-comms-setup.sh")
        (fake_repo / "bin" / "slack-send.py").write_text(_SEND_STUB)
        (fake_repo / "bin" / "assistant-doctor.py").write_text(_DOCTOR_STUB)

        curl = stubbin / "curl"
        curl.write_text(_CURL_STUB)
        curl.chmod(0o755)

        if seed_config is not None:
            (home / ".assistant" / "config.json").write_text(json.dumps(seed_config))

        send_log = root / "send.log"
        doctor_log = root / "doctor.log"
        py_bin = str(Path(sys.executable).parent)
        env = {
            "HOME": str(home),
            "PATH": f"{stubbin}:{py_bin}:/usr/bin:/bin",
            "FAKE_AUTH_JSON": auth_json,
            "SEND_LOG": str(send_log),
            "DOCTOR_LOG": str(doctor_log),
            "SEND_EXIT": str(send_exit),
            "DOCTOR_EXIT": str(doctor_exit),
        }
        if token is not None:
            env["SLACK_BOT_TOKEN"] = token
        if ping_target is not None:
            env["SLACK_PING_TARGET"] = ping_target

        proc = subprocess.run(
            ["zsh", str(fake_repo / "bin" / "assistant-comms-setup.sh")],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        send_lines = send_log.read_text().splitlines() if send_log.exists() else []
        doctor_lines = doctor_log.read_text().splitlines() if doctor_log.exists() else []
        return proc, home, send_lines, doctor_lines

    def _config(self, home: Path) -> dict:
        p = home / ".assistant" / "config.json"
        return json.loads(p.read_text()) if p.exists() else {}

    # ── happy path ──────────────────────────────────────────────────────────

    def test_happy_path_writes_gated_config(self):
        proc, home, send_lines, doctor_lines = self._run(ping_target="C0TEST123")
        self.assertEqual(proc.returncode, 0, msg=proc.stdout)
        cfg = self._config(home)
        # THE send-gate invariant: allowed_targets is exactly [target].
        self.assertEqual(cfg["slack"]["target"], "C0TEST123")
        self.assertEqual(cfg["slack"]["allowed_targets"], ["C0TEST123"])
        # Token is NEVER written to config.json.
        self.assertNotIn("xoxb", json.dumps(cfg))
        self.assertNotIn("bot_token", json.dumps(cfg).lower())

    def test_config_is_chmod_600(self):
        _, home, _, _ = self._run()
        mode = stat.S_IMODE((home / ".assistant" / "config.json").stat().st_mode)
        self.assertEqual(mode, 0o600, f"config.json must be 0600, got {oct(mode)}")

    def test_test_send_goes_to_the_configured_target(self):
        _, _, send_lines, _ = self._run(ping_target="C0TEST123")
        self.assertEqual(len(send_lines), 1, "expected exactly one test send")
        argv = json.loads(send_lines[0])
        self.assertIn("--channel", argv)
        self.assertEqual(argv[argv.index("--channel") + 1], "C0TEST123")

    def test_preserves_unrelated_existing_config_keys(self):
        seed = {"stale_heartbeat_sec": 999, "daemon": {"cadence": 7},
                "slack": {"target": "COLD", "allowed_targets": ["COLD"]}}
        _, home, _, _ = self._run(ping_target="C0NEW456", seed_config=seed)
        cfg = self._config(home)
        # unrelated keys survive the merge …
        self.assertEqual(cfg["daemon"], {"cadence": 7})
        # … and the target is updated + the gate follows it.
        self.assertEqual(cfg["slack"]["target"], "C0NEW456")
        self.assertEqual(cfg["slack"]["allowed_targets"], ["C0NEW456"])

    def test_doctor_preflight_is_invoked_slack_strict(self):
        _, _, _, doctor_lines = self._run()
        self.assertTrue(any("--only slack" in l and "--strict" in l
                            for l in doctor_lines),
                        f"doctor not run with --only slack --strict: {doctor_lines}")

    # ── failure branches ────────────────────────────────────────────────────

    def test_missing_token_aborts_before_writing_config(self):
        proc, home, send_lines, _ = self._run(token=None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SLACK_BOT_TOKEN", proc.stdout)
        # nothing sent, no config written — a clean early abort.
        self.assertEqual(send_lines, [])
        self.assertFalse((home / ".assistant" / "config.json").exists())

    def test_auth_failure_aborts_before_writing_config(self):
        proc, home, send_lines, _ = self._run(auth_json=FAIL_AUTH)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("auth.test failed", proc.stdout)
        self.assertEqual(send_lines, [])
        self.assertFalse((home / ".assistant" / "config.json").exists())

    def test_send_failure_aborts_nonzero(self):
        # config IS written (step 3) before the test-send (step 4); a send
        # failure must surface as a nonzero exit, not a silent success.
        proc, home, _, _ = self._run(send_exit=1)
        self.assertEqual(proc.returncode, 1)
        self.assertTrue((home / ".assistant" / "config.json").exists())

    def test_doctor_failure_warns_but_does_not_abort(self):
        # A failed preflight must NOT nuke the config the user just set up; it
        # warns and tells them to fix + reload. Script still exits 0.
        proc, home, _, _ = self._run(doctor_exit=1)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout)
        self.assertIn("preflight check FAILED", proc.stdout)
        self.assertTrue((home / ".assistant" / "config.json").exists())

    def tearDown(self):
        td = getattr(self, "_td", None)
        if td is not None:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
