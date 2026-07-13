from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO / "bin/llm_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("llm_runner_test_mod",
                                                  str(RUNNER_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class RouteConfigTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_runner()
        self._tmp_obj = TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)
        self.path = self.tmp / "config.json"

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_missing_config_defaults_to_claude(self):
        # Fail-closed to the always-present agent: a missing config must NOT
        # route the fleet at a droid binary that may be absent (which fails open
        # to empty verdicts every pulse). Droid is opt-in.
        cfg = self.mod.load_route_config(self.path, env={})
        self.assertEqual(cfg.provider, "claude")
        self.assertEqual(cfg.droid_canary_percent, 100)
        self.assertEqual(cfg.droid_model, "glm-5.2")

    def test_config_path_honors_shared_override(self):
        self.assertEqual(
            self.mod.config_path(home=self.tmp, env={}),
            self.tmp / ".assistant/comms/config.json",
        )
        self.assertEqual(
            self.mod.config_path(
                home=self.tmp,
                env={"ASSISTANT_LLM_CONFIG": str(self.path)},
            ),
            self.path,
        )

    def test_invalid_config_fails_closed_to_claude(self):
        self.path.write_text(json.dumps({
            "triage": {
                "provider": "unknown",
                "droid_canary_percent": 500,
                "droid_reasoning_effort": "low",
            },
        }))
        cfg = self.mod.load_route_config(self.path, env={})
        self.assertEqual(cfg.provider, "claude")
        self.assertEqual(cfg.droid_canary_percent, 100)
        self.assertEqual(cfg.droid_reasoning_effort, "high")

    def test_environment_overrides_config(self):
        self.path.write_text(json.dumps({
            "triage": {"provider": "claude", "droid_canary_percent": 0},
        }))
        cfg = self.mod.load_route_config(self.path, env={
            "TRIAGE_LLM_PROVIDER": "canary",
            "TRIAGE_DROID_CANARY_PERCENT": "25",
            "TRIAGE_DROID_MODEL": "glm-5.2-fast",
            "TRIAGE_DROID_REASONING_EFFORT": "max",
            "DROID_BIN": "/opt/droid",
        })
        self.assertEqual(cfg.provider, "canary")
        self.assertEqual(cfg.droid_canary_percent, 25)
        self.assertEqual(cfg.droid_model, "glm-5.2-fast")
        self.assertEqual(cfg.droid_reasoning_effort, "max")
        self.assertEqual(cfg.droid_bin, "/opt/droid")

    def test_global_provider_overrides_legacy_feature_config(self):
        self.path.write_text(json.dumps({
            "llm": {
                "provider": "droid",
                "droid": {
                    "bin": "/global/droid",
                    "model": "glm-5.2-fast",
                    "reasoning_effort": "max",
                },
            },
            "triage": {"provider": "claude"},
        }))
        cfg = self.mod.load_route_config(self.path, env={})
        self.assertEqual(cfg.provider, "droid")
        self.assertEqual(cfg.droid_bin, "/global/droid")
        self.assertEqual(cfg.droid_model, "glm-5.2-fast")
        self.assertEqual(cfg.droid_reasoning_effort, "max")

    def test_feature_provider_overrides_global_provider(self):
        self.path.write_text(json.dumps({
            "llm": {
                "provider": "claude",
                "features": {
                    "triage": {
                        "provider": "canary",
                        "droid_canary_percent": 30,
                        "droid": {"model": "glm-5.2-fast"},
                    },
                },
            },
        }))
        cfg = self.mod.load_route_config(self.path, env={})
        self.assertEqual(cfg.provider, "canary")
        self.assertEqual(cfg.droid_canary_percent, 30)
        self.assertEqual(cfg.droid_model, "glm-5.2-fast")

    def test_canary_route_is_stable_and_bounded(self):
        bucket = self.mod.canary_bucket(["event-2", "event-1"])
        self.assertEqual(bucket,
                         self.mod.canary_bucket(["event-1", "event-2"]))
        cfg = self.mod.RouteConfig(provider="canary",
                                   droid_canary_percent=bucket)
        self.assertEqual(self.mod.select_provider(cfg, ["event-1", "event-2"]),
                         "claude")
        cfg = self.mod.RouteConfig(provider="canary",
                                   droid_canary_percent=bucket + 1)
        self.assertEqual(self.mod.select_provider(cfg, ["event-1", "event-2"]),
                         "droid")


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_runner()

    def test_parses_claude_envelope(self):
        parsed = self.mod.parse_result_envelope("claude", json.dumps({
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "output_tokens": 4,
            },
            "total_cost_usd": 0.25,
        }))
        self.assertEqual(parsed["tokens_in"], 60)
        self.assertEqual(parsed["tokens_out"], 4)
        self.assertEqual(parsed["cost_usd"], 0.25)

    def test_rejects_failed_claude_envelope(self):
        envelope = json.dumps({
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })
        parsed = self.mod.parse_result_envelope("claude", envelope)
        self.assertIsNone(parsed)
        self.assertEqual(
            self.mod.parse_usage_envelope(envelope)["tokens_in"], 10)

    def test_parses_successful_droid_envelope(self):
        parsed = self.mod.parse_result_envelope("droid", json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-1",
            "result": '{"event_id":"e1","suggested_lane":"digest"}',
            "usage": {"input_tokens": 100, "output_tokens": 8},
        }))
        self.assertEqual(parsed["tokens_in"], 100)
        self.assertEqual(parsed["tokens_out"], 8)
        self.assertIsNone(parsed["cost_usd"])
        self.assertEqual(parsed["session_id"], "session-1")
        self.assertIn("event_id", parsed["result_text"])

    def test_rejects_failed_or_malformed_droid_envelope(self):
        failed = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "usage": {"input_tokens": 100},
        }
        self.assertIsNone(self.mod.parse_result_envelope(
            "droid", json.dumps(failed)))
        self.assertIsNone(self.mod.parse_result_envelope("droid", "not json"))


class InvokeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_runner()
        self._tmp_obj = TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_invokes_droid_with_glm_read_only(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen.update(kwargs)
            return 0, json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "droid-session",
                "result": "done",
                "usage": {"input_tokens": 50, "output_tokens": 5},
            }), ""

        result = self.mod.invoke(
            provider="droid",
            prompt="triage",
            model="glm-5.2",
            run_dir=self.tmp,
            timeout=30,
            run=fake_run,
            claude_bin="/claude",
            droid_bin="/droid",
            json_schema={"type": "object"},
        )
        self.assertTrue(result.usable)
        self.assertEqual(result.provider, "droid")
        self.assertEqual(result.result_text, "done")
        self.assertEqual(seen["cmd"][:4],
                         ["/droid", "exec", "--model", "glm-5.2"])
        self.assertEqual(
            seen["cmd"][seen["cmd"].index("--auto") + 1], "high")
        self.assertNotIn("--skip-permissions-unsafe", seen["cmd"])
        self.assertNotIn("--json-schema", seen["cmd"])
        self.assertEqual(seen["input_text"], "triage")
        self.assertFalse(seen["merge_bedrock"])

    def test_invalid_provider_fails_closed_to_claude(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return 0, json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }), ""

        result = self.mod.invoke(
            provider="unknown", prompt="triage", model="a-model",
            run_dir=self.tmp, timeout=30, run=fake_run,
            claude_bin="/claude", droid_bin="/droid",
        )
        # Unrecognized provider coerces to claude (fail-closed to the
        # always-present agent), not droid — an opt-in binary that may be absent.
        self.assertEqual(result.provider, "claude")
        self.assertEqual(seen["cmd"][0], "/claude")

    def test_invokes_claude_with_existing_contract(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen.update(kwargs)
            return 0, json.dumps({
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "total_cost_usd": 0.01,
            }), ""

        result = self.mod.invoke(
            provider="claude",
            prompt="triage",
            model="sonnet",
            run_dir=self.tmp,
            timeout=30,
            run=fake_run,
            claude_bin="/claude",
        )
        self.assertTrue(result.usable)
        self.assertEqual(seen["cmd"][0], "/claude")
        self.assertIn("--add-dir", seen["cmd"])
        self.assertTrue(seen["merge_bedrock"])

    def test_no_tools_droid_enumerates_and_disables_every_allowed_tool(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append((cmd, kwargs))
            if "--list-tools" in cmd:
                return 0, json.dumps([
                    {"id": "read-cli", "currentlyAllowed": True},
                    {"id": "execute-cli", "currentlyAllowed": True},
                    {"id": "write-cli", "currentlyAllowed": False},
                ]), ""
            return 0, json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "usage": {"input_tokens": 8, "output_tokens": 1},
            }), ""

        result = self.mod.invoke(
            provider="droid", prompt="triage", model="glm-5.2",
            run_dir=self.tmp, timeout=30, run=fake_run,
            claude_bin="/claude", droid_bin="/droid",
            disable_tools=True,
        )
        self.assertTrue(result.usable)
        self.assertEqual(len(seen), 2)
        command = seen[1][0]
        disabled = command[command.index("--disabled-tools") + 1]
        self.assertEqual(set(disabled.split(",")),
                         {"read-cli", "execute-cli"})

    def test_no_tools_droid_fails_closed_when_enumeration_fails(self):
        calls = 0

        def fake_run(cmd, **kwargs):
            nonlocal calls
            calls += 1
            return 1, "", "not authenticated"

        result = self.mod.invoke(
            provider="droid", prompt="triage", model="glm-5.2",
            run_dir=self.tmp, timeout=30, run=fake_run,
            claude_bin="/claude", droid_bin="/missing/droid",
            disable_tools=True,
        )
        self.assertFalse(result.usable)
        self.assertEqual(result.rc, 126)
        self.assertEqual(calls, 1)

    def test_no_tools_claude_has_no_permission_bypass_or_mcp(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return 0, json.dumps({
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "result": "done",
            }), ""

        result = self.mod.invoke(
            provider="claude", prompt="triage", model="sonnet",
            run_dir=self.tmp, timeout=30, run=fake_run,
            claude_bin="/claude", disable_tools=True,
            json_schema={
                "type": "object",
                "required": ["verdict"],
            },
        )
        self.assertTrue(result.usable)
        self.assertNotIn("--dangerously-skip-permissions", seen["cmd"])
        self.assertEqual(
            seen["cmd"][seen["cmd"].index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", seen["cmd"])
        schema = seen["cmd"][seen["cmd"].index("--json-schema") + 1]
        self.assertEqual(json.loads(schema), {
            "type": "object",
            "required": ["verdict"],
        })

    def test_failed_call_preserves_observed_usage(self):
        def fake_run(cmd, **kwargs):
            return 1, json.dumps({
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "usage": {"input_tokens": 100, "output_tokens": 5},
            }), "failed"

        result = self.mod.invoke(
            provider="claude", prompt="triage", model="sonnet",
            run_dir=self.tmp, timeout=30, run=fake_run,
            claude_bin="/claude", disable_tools=True,
        )
        self.assertFalse(result.usable)
        self.assertEqual(result.tokens_in, 100)
        self.assertEqual(result.tokens_out, 5)
        self.assertEqual(result.usage_source, "cli")


if __name__ == "__main__":
    unittest.main()
