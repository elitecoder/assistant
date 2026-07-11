"""Tests for the Keel M7 brief narrator (src/assistant/narrator.py + the
bin/narrate-brief.py subprocess caller + the renderer's editorial overlay).

The narrator is the first LLM to touch the morning brief, so the suite proves —
with NO live LLM and NO network (every narration is INJECTED) — that it can
NEVER compromise the brief's purity or invent facts:

  • the brief stays a PURE derivation: build_brief bytes are identical whether or
    not a narrative sidecar exists (the narrative lives in a SEPARATE sidecar);
  • GROUNDING: validate_narrative drops any recommendation keyed to a decision id
    the brief never surfaced (the structural M7 twin of M6's playbook-enum guard);
  • FALLBACK: gate closed / raising / malformed LLM → deterministic template
    floor, always a narrative, never a crash;
  • the narrative is EPOCH-TIED: a stale sidecar (brief rebuilt) is ignored;
  • TEXT ONLY: the narrative dict carries no field that can act;
  • the renderer overlays the voice and degrades to the template with no sidecar.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
BIN = REPO / "bin"
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))

from assistant import brief, narrator, strategist  # noqa: E402


def _load_cli():
    """Load bin/narrate-brief.py (hyphenated → by path, as the pulse does)."""
    spec = importlib.util.spec_from_file_location(
        "narrate_brief", str(BIN / "narrate-brief.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _brief_doc(now: float) -> dict:
    return {
        "schema": "morning-brief/1", "date": "2026-07-10",
        "ts": "2026-07-10T14:00:00Z", "epoch": int(now),
        "queue": [
            {"id": "dec-11", "title": "Merge PR #9", "lane": "escalate",
             "urgency": "now", "policy_id": "cmux-escalate",
             "default_label": "Accept: merge", "score": 126, "age_h": 6.2,
             "snippet": "policy engine"},
            {"id": "dec-22", "title": "Deploy to fleet", "lane": "staged",
             "urgency": "high", "policy_id": "goal-plan",
             "default_label": "Accept", "score": 72, "age_h": 3.0,
             "snippet": "git pull"},
        ],
        "handled_overnight": [
            {"ts": "2026-07-10T04:12:00Z", "kind": "merge-dispatched",
             "key": "pr-7", "evidence": "PR #7 merged", "ws_ref": "assistant"}],
        "digest": {},
        "health": {"cost": {"cost_per_day_usd": 4.2},
                   "interrupts": {"delivered_24h": 0, "denied_24h": 3,
                                  "budget": {"page": 0, "notify": 3}},
                   "expired_unseen_24h": 0, "connectors": {}},
        "counts": {"open_decisions": 2,
                   "by_lane": {"escalate": 1, "staged": 1},
                   "handled_overnight": 1, "digest_rows": 0},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = Path(self._tmp_obj.name)
        (self._tmp / ".assistant").mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self._tmp)
        self.now = time.time()
        self.doc = _brief_doc(self.now)
        # Default: the Strategist gate is OPEN unless a test closes it.
        self._real_active = strategist.active
        strategist.active = lambda now, records=None: (True, None)

    def tearDown(self):
        strategist.active = self._real_active
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()


class GroundingTests(_Base):
    def test_deterministic_floor_always_present(self):
        """No LLM: a template summary + one line per top decision, from counts."""
        narr = narrator.build_narrative(
            self.doc, llm_narrate=lambda facts: None, now=self.now)
        self.assertEqual(narr["source"], "template")
        self.assertIn("1 handled overnight", narr["summary"])
        self.assertIn("2 decisions for you", narr["summary"])
        self.assertEqual(set(narr["recommendations"]), {"dec-11", "dec-22"})

    def test_facts_expose_only_surfaced_ids(self):
        facts = narrator.brief_facts(self.doc)
        self.assertEqual([d["id"] for d in facts["decisions"]],
                         ["dec-11", "dec-22"])
        # counts are the brief's, not re-derived
        self.assertEqual(facts["counts"]["by_lane"], {"escalate": 1, "staged": 1})

    def test_validate_drops_invented_decision_id(self):
        raw = {"summary": "Good morning.",
               "recommendations": {"dec-11": "merge it",
                                   "dec-NOPE": "invented — must drop",
                                   "dec-22": ""}}  # empty dropped too
        valid = narrator.validate_narrative(raw, self.doc)
        self.assertEqual(valid["recommendations"], {"dec-11": "merge it"})

    def test_validate_rejects_missing_summary(self):
        self.assertIsNone(narrator.validate_narrative(
            {"recommendations": {}}, self.doc))
        self.assertIsNone(narrator.validate_narrative("not a dict", self.doc))

    def test_llm_overlay_keeps_template_line_for_omitted_decisions(self):
        raw = {"summary": "Good morning. One merge waits.",
               "recommendations": {"dec-11": "skim commit 2, admin-merge"}}
        narr = narrator.build_narrative(
            self.doc, llm_narrate=lambda facts: raw, now=self.now)
        self.assertEqual(narr["source"], "llm")
        self.assertEqual(narr["summary"], "Good morning. One merge waits.")
        self.assertEqual(narr["recommendations"]["dec-11"], "skim commit 2, admin-merge")
        # dec-22 omitted by the LLM → keeps its deterministic template line
        self.assertIn("dec-22", narr["recommendations"])
        self.assertTrue(narr["recommendations"]["dec-22"])


class FallbackTests(_Base):
    def test_gate_closed_returns_template_floor(self):
        strategist.active = lambda now, records=None: (False, "ceiling-shed")
        called = []
        narr = narrator.build_narrative(
            self.doc, llm_narrate=lambda f: called.append(1) or {}, now=self.now)
        self.assertEqual(narr["source"], "template")
        self.assertEqual(narr["reason"], "ceiling-shed")
        self.assertEqual(called, [])  # gate closed BEFORE any spend

    def test_raising_llm_falls_back(self):
        def boom(facts):
            raise RuntimeError("subprocess died")
        narr = narrator.build_narrative(
            self.doc, llm_narrate=boom, now=self.now)
        self.assertEqual(narr["source"], "template")
        self.assertEqual(narr["reason"], "llm-error")

    def test_malformed_llm_falls_back(self):
        narr = narrator.build_narrative(
            self.doc, llm_narrate=lambda f: {"no_summary": 1}, now=self.now)
        self.assertEqual(narr["source"], "template")
        self.assertEqual(narr["reason"], "llm-invalid")

    def test_gate_unavailable_is_not_load_bearing(self):
        def broken_active(now, records=None):
            raise RuntimeError("strategist import broke")
        strategist.active = broken_active
        narr = narrator.build_narrative(
            self.doc, llm_narrate=lambda f: {"summary": "x"}, now=self.now)
        self.assertEqual(narr["source"], "template")
        self.assertEqual(narr["reason"], "gate-unavailable")


class TextOnlyTests(_Base):
    def test_narrative_dict_carries_no_action_field(self):
        """The narrative can PHRASE but not ACT: no lane/action/dispatch key can
        survive validation, no matter what the LLM returns."""
        raw = {"summary": "hi", "lane": "auto", "action": "merge",
               "dispatch": True, "auto": "yes",
               "recommendations": {"dec-11": "ok"}}
        valid = narrator.validate_narrative(raw, self.doc)
        self.assertEqual(set(valid), {"summary", "recommendations"})


class SidecarTests(_Base):
    def test_epoch_match_overlays_but_mismatch_falls_back(self):
        narrator.write_narrative({
            "schema": "brief-narrative/1", "date": "2026-07-10",
            "brief_epoch": int(self.now), "source": "llm",
            "summary": "Good morning, voiced.",
            "recommendations": {"dec-11": "merge"}})
        # matching epoch → sidecar voice
        got = narrator.narrative_for_brief(self.doc)
        self.assertEqual(got["source"], "llm")
        self.assertEqual(got["summary"], "Good morning, voiced.")
        # every rendered decision still has a line (template backfills dec-22)
        self.assertIn("dec-22", got["recommendations"])
        # rebuilt brief with a changed queue → stale sidecar ignored
        stale = dict(self.doc, epoch=int(self.now) + 999)
        self.assertEqual(narrator.narrative_for_brief(stale)["source"], "template")


class PurityTests(_Base):
    def test_brief_bytes_identical_with_and_without_narrative(self):
        """The brief is a PURE derivation: writing a narrative sidecar must not
        change a single byte of the rebuilt brief (M7's core invariant)."""
        b1 = brief.build_brief(now=self.now)
        narrator.write_narrative(narrator.build_narrative(
            b1, llm_narrate=lambda f: {"summary": "voiced",
                                       "recommendations": {}}, now=self.now))
        b2 = brief.build_brief(now=self.now)
        self.assertEqual(json.dumps(b1, sort_keys=True),
                         json.dumps(b2, sort_keys=True))
        # and the narrative file is a DISTINCT path from the brief file
        self.assertNotEqual(narrator.narrative_path("2026-07-10"),
                            brief.brief_path("2026-07-10"))


class CliTests(_Base):
    def _cli_with_fake_spawn(self, payload):
        cli = _load_cli()

        def fake_spawn(prompt, run_dir, out_name):
            Path(run_dir).mkdir(parents=True, exist_ok=True)
            (Path(run_dir) / out_name).write_text(json.dumps(payload))
            return 0, "{}", ""
        cli._spawn = fake_spawn
        return cli

    def test_generate_writes_sidecar_and_dedups_by_stamp(self):
        brief.write_brief(self.doc)
        cli = self._cli_with_fake_spawn({
            "summary": "Good morning. One merge, one deploy.",
            "recommendations": {"dec-11": "merge ~10 min", "dec-X": "drop me"}})
        out = cli.generate(now=self.now, force=True)
        self.assertTrue(out["written"])
        self.assertEqual(out["source"], "llm")
        side = narrator.read_narrative("2026-07-10")
        self.assertEqual(side["summary"], "Good morning. One merge, one deploy.")
        self.assertNotIn("dec-X", side["recommendations"])  # grounding held
        # second run without --force no-ops on the per-date stamp
        out2 = cli.generate(now=self.now)
        self.assertFalse(out2["written"])
        self.assertEqual(out2["reason"], "already-narrated")

    def test_generate_no_brief_is_a_clean_noop(self):
        cli = self._cli_with_fake_spawn({"summary": "x"})
        out = cli.generate(now=self.now, force=True)
        self.assertFalse(out["written"])
        self.assertEqual(out["reason"], "no-brief")

    def test_read_json_obj_tolerates_fences_and_junk(self):
        cli = _load_cli()
        d = self._tmp / "n.json"
        d.write_text("```json\n{\"summary\": \"hi\"}\n```")
        self.assertEqual(cli.read_json_obj(d), {"summary": "hi"})
        d.write_text("not json at all")
        self.assertIsNone(cli.read_json_obj(d))


class PulseWiringTests(_Base):
    def _load_pulse(self):
        spec = importlib.util.spec_from_file_location(
            "pulse_mod", str(BIN / "pulse.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_narrator_step_is_fenced_noop_without_a_brief(self):
        """run_narrator_step must never raise and must never spawn when there's
        no brief yet — a broken/absent voice can't break the pulse (nor reach the
        network: generate() returns 'no-brief' before any subprocess)."""
        pulse = self._load_pulse()
        # No brief on disk → clean no-op, no exception, no network.
        pulse.run_narrator_step(pulse_idx=0)
        self.assertFalse(narrator.narrative_path("2026-07-10").exists())


if __name__ == "__main__":
    unittest.main()
