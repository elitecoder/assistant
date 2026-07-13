"""Direct-import tests for bin/save-ws-summary.py.

The existing tests via subprocess exercise the CLI shape but don't
contribute to coverage because subprocess.run launches a fresh Python
process. This test imports the module directly so coverage measures
every branch.

Pinned in particular: compute_state_hash collisions (the bug we
diagnosed earlier — the function reads legacy fields the new verdict
schema never writes, so 100+ summaries on disk all hash to the same
value f5d477a88b71fe4b). The pin asserts the current behavior so the
fix in a follow-up commit is a deliberate change.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/save-ws-summary.py"


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("save_ws_summary_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    return tmp


class StateHashTests(unittest.TestCase):
    """compute_state_hash collisions documented for follow-up fix.

    The function looks at:
      classification, summary_for_next_pulse, sorted proposed_actions kinds.

    The new schema writes:
      verdict, summary, next, ...

    So new-schema verdicts all hash to the same value. This is the bug
    diagnosed in the 2026-05-27 session — finding ~103/120 summaries
    sharing hash f5d477a88b71fe4b.

    Tests below pin this behavior so any change is intentional."""

    @classmethod
    def setUpClass(cls):
        cls._tmp_obj = TemporaryDirectory()
        cls._tmp = fixture_home(Path(cls._tmp_obj.name))
        cls.mod = load_module(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        cls._tmp_obj.cleanup()

    def test_legacy_fields_drive_hash(self):
        h_a = self.mod.compute_state_hash({
            "classification": "DONE",
            "summary_for_next_pulse": "x",
            "proposed_actions": [{"kind": "cleanup"}],
        })
        h_b = self.mod.compute_state_hash({
            "classification": "DONE",
            "summary_for_next_pulse": "x",
            "proposed_actions": [{"kind": "cleanup"}],
        })
        self.assertEqual(h_a, h_b)
        h_c = self.mod.compute_state_hash({
            "classification": "ACTIVE",
            "summary_for_next_pulse": "y",
            "proposed_actions": [],
        })
        self.assertNotEqual(h_a, h_c)

    def test_new_schema_collides_to_known_value(self):
        # Two completely different new-schema verdicts produce the SAME hash
        # because the legacy fields (classification, summary_for_next_pulse,
        # proposed_actions) are absent in both.
        h_a = self.mod.compute_state_hash({
            "verdict": "active", "summary": "doing X", "next": "Y",
        })
        h_b = self.mod.compute_state_hash({
            "verdict": "needs_user", "summary": "blocked Z", "next": "approve",
        })
        self.assertEqual(h_a, h_b)
        # The actual collision value we observed in the 0526 incident.
        # Pinning it means any change to compute_state_hash is a deliberate flip.
        self.assertEqual(h_a, "f5d477a88b71fe4b")

    def test_sorted_action_kinds_normalize(self):
        h_a = self.mod.compute_state_hash({
            "classification": "X", "summary_for_next_pulse": "s",
            "proposed_actions": [{"kind": "a"}, {"kind": "b"}],
        })
        h_b = self.mod.compute_state_hash({
            "classification": "X", "summary_for_next_pulse": "s",
            "proposed_actions": [{"kind": "b"}, {"kind": "a"}],
        })
        self.assertEqual(h_a, h_b)

    def test_handles_none_action_entry(self):
        # Defensive against {"kind": "..."} | None entries — coverage hits the
        # `(a or {})` branch.
        h = self.mod.compute_state_hash({
            "classification": "X", "summary_for_next_pulse": "s",
            "proposed_actions": [None, {"kind": "k"}],
        })
        self.assertIsInstance(h, str)


def run_main(home: Path, argv: list[str]) -> tuple[int, str, str]:
    """Invoke save-ws-summary's main() with sys.argv = [script, *argv].
    Captures stdout/stderr.

    Each call re-loads the module so the HOME-rooted CACHE_DIR is bound to
    `home` for this test."""
    import io
    mod = load_module(home)
    sys.argv = ["save-ws-summary.py"] + argv
    captured_out, captured_err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", captured_out), mock.patch("sys.stderr", captured_err):
        rc = mod.main()
    return rc, captured_out.getvalue(), captured_err.getvalue()


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_writes_summary_with_next(self):
        rc, out, err = run_main(self._tmp, [
            "--ws-ref", "workspace:7",
            "--title", "test",
            "--cwd", "/tmp",
            "--pr-refs", "[1234]",
            "--json", json.dumps({
                "verdict": "active", "summary": "s", "next": "n"}),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("saved:", out)
        path = self._tmp / ".assistant/observer-summaries/workspace_7.json"
        self.assertTrue(path.exists())
        d = json.loads(path.read_text())
        self.assertEqual(d["verdict"], "active")
        self.assertEqual(d["pr_refs"], [1234])
        self.assertEqual(d["ws_ref"], "workspace:7")

    def test_rejects_invalid_json(self):
        rc, _, err = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", "{not json",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("failed to parse", err)

    def test_rejects_non_object_json(self):
        rc, _, err = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", "[1,2,3]",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("must be a JSON object", err)

    def test_synthesizes_missing_next(self):
        rc, _, err = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({"verdict": "active", "summary": "s"}),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("synthesized fallback", err)
        saved = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        self.assertEqual(saved["next"], "(inferred) s")

    def test_synthesizes_blank_next(self):
        rc, _, err = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({"verdict": "active", "summary": "s", "next": "   "}),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("synthesized fallback", err)
        saved = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        self.assertEqual(saved["next"], "(inferred) s")

    def test_synthesizes_next_for_non_string_fields(self):
        rc, _, err = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({"verdict": "active", "summary": {"bad": "shape"}, "next": 1}),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("synthesized fallback", err)
        saved = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        self.assertIn("unknown", saved["next"])

    def test_pr_refs_default_to_empty_when_unparseable(self):
        rc, _, _ = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--pr-refs", "{this is not a json array",
            "--json", json.dumps({"verdict": "active", "summary": "s", "next": "n"}),
        ])
        self.assertEqual(rc, 0)
        d = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        self.assertEqual(d["pr_refs"], [])

    def test_state_unchanged_since_carries_forward_when_hash_matches(self):
        # Write once.
        rc1, _, _ = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({"verdict": "active", "summary": "s", "next": "n"}),
        ])
        self.assertEqual(rc1, 0)
        first = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        first_unchanged_since = first["state_unchanged_since_ts"]
        # Write again with same legacy-hash inputs (i.e. same hash collision).
        rc2, _, _ = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({"verdict": "no_action", "summary": "different", "next": "different"}),
        ])
        self.assertEqual(rc2, 0)
        second = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        # Because compute_state_hash collides, the unchanged-since timestamp
        # is preserved across the two writes.
        self.assertEqual(second["state_unchanged_since_ts"], first_unchanged_since)

    def test_state_unchanged_since_resets_when_legacy_hash_differs(self):
        rc1, _, _ = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({
                "verdict": "active", "summary": "s", "next": "n",
                "classification": "ACTIVE",
                "summary_for_next_pulse": "first",
                "proposed_actions": [{"kind": "a"}],
            }),
        ])
        first = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        # Wait one second so we can compare timestamps.
        import time as _t
        _t.sleep(1)
        rc2, _, _ = run_main(self._tmp, [
            "--ws-ref", "workspace:1",
            "--json", json.dumps({
                "verdict": "active", "summary": "s", "next": "n",
                "classification": "STRANDED",  # changed
                "summary_for_next_pulse": "second",
                "proposed_actions": [{"kind": "b"}],
            }),
        ])
        second = json.loads(
            (self._tmp / ".assistant/observer-summaries/workspace_1.json").read_text()
        )
        self.assertGreater(second["state_unchanged_since_ts"], first["state_unchanged_since_ts"])

    def test_corrupt_prior_file_is_ignored(self):
        # Plant a corrupt prior summary; main() should swallow + overwrite.
        path = self._tmp / ".assistant/observer-summaries/workspace_5.json"
        path.write_text("{ not json")
        rc, _, _ = run_main(self._tmp, [
            "--ws-ref", "workspace:5",
            "--json", json.dumps({"verdict": "active", "summary": "s", "next": "n"}),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("verdict", path.read_text())


if __name__ == "__main__":
    unittest.main()
