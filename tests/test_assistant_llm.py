from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin/assistant-llm.py"


class AssistantLLMCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)
        self.config = self.tmp / "config.json"
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.tmp)
        self.env["ASSISTANT_LLM_CONFIG"] = str(self.config)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            env=self.env,
            text=True,
            capture_output=True,
        )

    def test_missing_config_defaults_to_droid(self):
        result = self.run_cli("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["global_provider"], "droid")
        self.assertEqual(status["features"]["triage"]["provider"], "droid")

    def test_set_droid_preserves_unrelated_and_legacy_config(self):
        self.config.write_text(json.dumps({
            "daemon": {"pulse_interval_sec": 60},
            "triage": {"provider": "canary",
                       "droid_canary_percent": 10},
        }))
        result = self.run_cli("set", "droid")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(self.config.read_text())
        self.assertEqual(document["daemon"]["pulse_interval_sec"], 60)
        self.assertEqual(document["triage"]["provider"], "canary")
        self.assertEqual(document["llm"]["provider"], "droid")
        self.assertEqual(document["llm"]["droid"]["model"], "glm-5.2")
        self.assertIn("triage: droid", result.stdout)

    def test_set_claude_is_immediate_rollback(self):
        self.assertEqual(self.run_cli("set", "droid").returncode, 0)
        result = self.run_cli("set", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(self.run_cli("status", "--json").stdout)
        self.assertEqual(status["global_provider"], "claude")
        self.assertEqual(status["features"]["triage"]["provider"], "claude")

    def test_status_surfaces_environment_override(self):
        self.env["TRIAGE_LLM_PROVIDER"] = "droid"
        result = self.run_cli("set", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "overridden by TRIAGE_LLM_PROVIDER=droid", result.stdout)
        status = json.loads(self.run_cli("status", "--json").stdout)
        self.assertEqual(status["global_provider"], "claude")
        self.assertEqual(status["features"]["triage"]["provider"], "droid")
        self.assertEqual(
            status["features"]["triage"]["environment_override"], "droid")

    def test_feature_override_and_inherit(self):
        self.assertEqual(self.run_cli("set", "droid").returncode, 0)
        result = self.run_cli(
            "set", "canary", "--feature", "triage", "--percent", "25")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(self.run_cli("status", "--json").stdout)
        self.assertEqual(status["features"]["triage"]["provider"], "canary")
        self.assertEqual(
            status["features"]["triage"]["droid_canary_percent"], 25)

        result = self.run_cli("inherit", "--feature", "triage")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(self.run_cli("status", "--json").stdout)
        self.assertEqual(status["features"]["triage"]["provider"], "droid")

    def test_global_canary_percentage_is_effective(self):
        result = self.run_cli("set", "canary", "--percent", "40")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(self.run_cli("status", "--json").stdout)
        self.assertEqual(status["features"]["triage"]["provider"], "canary")
        self.assertEqual(
            status["features"]["triage"]["droid_canary_percent"], 40)

    def test_invalid_canary_percent_does_not_write(self):
        result = self.run_cli("set", "canary", "--percent", "101")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.config.exists())

    def test_malformed_config_is_not_overwritten(self):
        self.config.write_text("{broken")
        result = self.run_cli("set", "droid")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.config.read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
