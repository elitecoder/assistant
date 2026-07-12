"""Tests for src/assistant/model_tiers.py (Keel M8) — the single model-id
resolver that ends the scattered, Bedrock-specific ids.

Proves:
  • semantic tiers resolve to the right id per provider (bedrock/anthropic/vertex);
  • the provider is detected off the SAME flags the CLI routes on — env first,
    then a ~/.zprofile fallback — and MODEL_PROVIDER forces it;
  • a per-tier ASSISTANT_MODEL_<TIER> override wins (operator pins an exact id);
  • the [1m] 1M-context suffix is Bedrock-only AND opt-in (never on anthropic,
    never when not requested — the mem0 quirk);
  • an unknown tier is a hard error, not a silent default.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))

from assistant import model_tiers as mt  # noqa: E402

_KEYS = ("MODEL_PROVIDER", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
         "ASSISTANT_MODEL_FRONTIER", "ASSISTANT_MODEL_BALANCED",
         "ASSISTANT_MODEL_CHEAP", "HOME")


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _KEYS}
        for k in _KEYS:
            os.environ.pop(k, None)
        # a HOME with no ~/.zprofile so detection can't leak the dev machine's flag
        self._tmp = TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class ProviderDetectionTests(_Base):
    def test_defaults_to_anthropic_when_nothing_indicates_a_cloud_backend(self):
        self.assertEqual(mt.provider(), "anthropic")

    def test_bedrock_env_flag(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        self.assertEqual(mt.provider(), "bedrock")

    def test_vertex_env_flag(self):
        os.environ["CLAUDE_CODE_USE_VERTEX"] = "true"
        self.assertEqual(mt.provider(), "vertex")

    def test_model_provider_forces_and_overrides_flags(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        os.environ["MODEL_PROVIDER"] = "anthropic"
        self.assertEqual(mt.provider(), "anthropic")

    def test_zprofile_fallback_when_env_unset(self):
        # launchd doesn't source ~/.zprofile, so the flag lives there — the
        # resolver must still find it (same reader shape as the pulse).
        (Path(self._tmp.name) / ".zprofile").write_text(
            'export FOO=bar\nexport CLAUDE_CODE_USE_BEDROCK="1"\n')
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", os.environ)
        self.assertEqual(mt.provider(), "bedrock")

    def test_zprofile_last_export_wins_and_trailing_comment_stripped(self):
        # A duplicated export → the shell (and load_bedrock_env) take the LAST;
        # a trailing comment must not defeat the flag (M8 review).
        (Path(self._tmp.name) / ".zprofile").write_text(
            "export CLAUDE_CODE_USE_BEDROCK=0\n"
            "export CLAUDE_CODE_USE_BEDROCK=1  # cloud host\n")
        self.assertEqual(mt.provider(), "bedrock")

    def test_falsey_flag_is_not_bedrock(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "0"
        self.assertEqual(mt.provider(), "anthropic")


class ResolutionTests(_Base):
    def test_bedrock_ids(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        # frontier is a CURRENT Opus (not the deprecated 4-1 the review flagged)
        self.assertEqual(mt.model_for("frontier"), "us.anthropic.claude-opus-4-8")
        self.assertEqual(mt.model_for("balanced"), "us.anthropic.claude-sonnet-4-6")
        self.assertEqual(mt.model_for("cheap"), "us.anthropic.claude-haiku-4-5")

    def test_anthropic_ids_are_bare(self):
        os.environ["MODEL_PROVIDER"] = "anthropic"
        self.assertEqual(mt.model_for("frontier"), "claude-opus-4-8")
        self.assertEqual(mt.model_for("cheap"), "claude-haiku-4-5")

    def test_provider_hint_pins_id_format_independent_of_detection(self):
        # mem0's LLM is hardcoded Bedrock even when the CLI routes elsewhere → it
        # passes provider_hint="bedrock" and must get a Bedrock id (M8 review M3).
        os.environ["MODEL_PROVIDER"] = "anthropic"
        self.assertEqual(mt.model_for("cheap", provider_hint="bedrock"),
                         "us.anthropic.claude-haiku-4-5")
        # an unknown hint falls back to detection
        self.assertEqual(mt.model_for("cheap", provider_hint="nonsense"),
                         "claude-haiku-4-5")

    def test_long_context_suffix_is_bedrock_only_and_opt_in(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        self.assertEqual(mt.model_for("balanced", long_context=True),
                         "us.anthropic.claude-sonnet-4-6[1m]")
        # not requested → no suffix (the mem0 path stays clean)
        self.assertNotIn("[1m]", mt.model_for("cheap"))
        # anthropic never gets the Bedrock-only suffix even if asked
        os.environ["MODEL_PROVIDER"] = "anthropic"
        self.assertNotIn("[1m]", mt.model_for("balanced", long_context=True))

    def test_per_tier_override_is_verbatim(self):
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        os.environ["ASSISTANT_MODEL_CHEAP"] = "my-org/tiny-model-v9"
        self.assertEqual(mt.model_for("cheap"), "my-org/tiny-model-v9")
        # VERBATIM — the operator pinned an exact id, so [1m] is NOT appended even
        # under long_context (they may have pinned it BECAUSE their path rejects
        # [1m]); if they want it they include it themselves (M8 review).
        self.assertEqual(mt.model_for("cheap", long_context=True),
                         "my-org/tiny-model-v9")

    def test_unknown_tier_raises(self):
        with self.assertRaises(ValueError):
            mt.model_for("supergenius")

    def test_pricing_substrings_survive_resolution(self):
        # metering matches by opus/sonnet/haiku substrings; every default id must
        # keep the family word so cost accounting works on any provider.
        for prov in ("bedrock", "anthropic", "vertex"):
            os.environ["MODEL_PROVIDER"] = prov
            self.assertIn("opus", mt.model_for("frontier"))
            self.assertIn("sonnet", mt.model_for("balanced"))
            self.assertIn("haiku", mt.model_for("cheap"))


if __name__ == "__main__":
    unittest.main()
