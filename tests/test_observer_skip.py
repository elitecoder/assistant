"""Tests for the Observer no-change skip (Keel M2, bin/pulse.py):

  - obs_input_hash pins exactly the Observer-visible state (transcript tail +
    screen + git signals) and excludes the always-growing age counter;
  - main() wiring: a workspace whose hash matches its last verdict's stored
    hash is carried forward with ZERO Observer calls, no action re-execution,
    and truthful metering (observer_called=false, skipped=N);
  - fixture replay over evals/observer/fixtures: with skip enabled an
    unchanged workspace reproduces its prior verdict byte-for-byte — zero
    divergence across all fixtures;
  - synthetic-day replay: a day where most pulses have no state change cuts
    Observer work by >=30% (the design's M2 eval bar).

unittest style so the suite runs under `python3 -m unittest discover tests`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PULSE_PATH = REPO / "bin/pulse.py"
FIXTURES = REPO / "evals/observer/fixtures"


def load_pulse(home: Path):
    """Import bin/pulse.py with HOME pointed at a tempdir (same pattern as
    test_pulse.py — its path constants bind at import). The Keel-M8 frontier
    shadow-audit is disabled here: it is an ORTHOGONAL, records-only feature
    that would spawn a second Observer call per pulse, and this suite counts
    Observer spawns to prove the no-change-skip wiring. Audit behaviour has its
    own suite (test_model_tiering.py)."""
    os.environ["HOME"] = str(home)
    os.environ["OBSERVER_AUDIT"] = "0"
    spec = importlib.util.spec_from_file_location("pulse_skip_mod",
                                                  str(PULSE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/inbox").mkdir(parents=True)
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    return tmp


BASE_CTX = {
    "ws_ref": "workspace:1", "title": "t", "cwd": "/",
    "transcript_path": None, "transcript_source": None, "session_id8": None,
    "agent_surface": None, "last_turn_age_sec": 100, "agent_status": "idle",
    "cwd_dirty": False, "cwd_unpushed": False, "is_protected": False,
    "screen_text": "the same screen", "screen_shows_error": False,
}


def ctx(**over) -> dict:
    d = dict(BASE_CTX)
    d.update(over)
    return d


# ─── hash semantics ──────────────────────────────────────────────────────────

class ObsInputHashTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = fixture_home(Path(self._tmp_obj.name))
        self._old_home = os.environ.get("HOME")
        self.mod = load_pulse(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def test_stable_for_identical_state(self):
        self.assertEqual(self.mod.obs_input_hash(ctx()),
                         self.mod.obs_input_hash(ctx()))

    def test_age_growth_within_a_band_never_changes_the_hash(self):
        # last_turn_age_sec grows every pulse with zero state change — if the
        # raw value fed the hash the skip would never engage. Growth on the
        # SAME side of the prompt's 1800s threshold must not change the hash.
        self.assertEqual(self.mod.obs_input_hash(ctx(last_turn_age_sec=100)),
                         self.mod.obs_input_hash(ctx(last_turn_age_sec=1700)))
        self.assertEqual(self.mod.obs_input_hash(ctx(last_turn_age_sec=1900)),
                         self.mod.obs_input_hash(ctx(last_turn_age_sec=90000)))

    def test_crossing_the_idle_threshold_changes_the_hash(self):
        # The Observer prompt's verdict rules flip at idle > 1800s (active →
        # stranded/ready_for_cleanup/needs_user). A hung-but-idle ws whose
        # only "change" is crossing that line MUST re-observe — otherwise an
        # `active` verdict carries forever and the recovery nudge never fires.
        self.assertNotEqual(
            self.mod.obs_input_hash(ctx(last_turn_age_sec=1700)),
            self.mod.obs_input_hash(ctx(last_turn_age_sec=1900)))
        # Boundary mirrors the prompt exactly: ≤1800 is one band, >1800 the
        # other ("strictly greater than 30 minutes").
        self.assertEqual(self.mod.idle_age_band(1800), "le1800")
        self.assertEqual(self.mod.idle_age_band(1801), "gt1800")
        self.assertEqual(self.mod.idle_age_band(None), "age-unknown")

    def test_each_observable_signal_changes_the_hash(self):
        base = self.mod.obs_input_hash(ctx())
        for over in ({"screen_text": "something new"},
                     {"agent_status": "working"},
                     {"cwd_dirty": True},
                     {"cwd_unpushed": True},
                     {"screen_shows_error": True},
                     {"title": "renamed"},
                     {"cwd": "/elsewhere"}):
            self.assertNotEqual(self.mod.obs_input_hash(ctx(**over)), base,
                                msg=str(over))

    def test_transcript_append_changes_the_hash(self):
        t = self.home / "transcript.jsonl"
        t.write_text('{"type": "user"}\n')
        c = ctx(transcript_path=str(t))
        before = self.mod.obs_input_hash(c)
        self.assertEqual(before, self.mod.obs_input_hash(c))  # stable first
        with open(t, "a") as f:
            f.write('{"type": "assistant"}\n')
        self.assertNotEqual(self.mod.obs_input_hash(c), before)

    def test_transcript_appearing_or_vanishing_changes_the_hash(self):
        t = self.home / "transcript.jsonl"
        t.write_text('{"type": "user"}\n')
        with_t = self.mod.obs_input_hash(ctx(transcript_path=str(t)))
        without = self.mod.obs_input_hash(ctx(transcript_path=None))
        self.assertNotEqual(with_t, without)

    def test_spinner_tick_does_not_change_the_hash(self):
        """A spinner animation frame (braille chars ⠋⠙⠹…) must not invalidate
        a carry-forward verdict — it is rendering jitter, not semantic change."""
        base = self.mod.obs_input_hash(ctx(screen_text="Working on task...\n⠋"))
        tick = self.mod.obs_input_hash(ctx(screen_text="Working on task...\n⠙"))
        self.assertEqual(base, tick,
                         "spinner frame change must not change the hash")

    def test_status_bar_clock_change_does_not_change_the_hash(self):
        """The status bar carries a volatile clock (2:34 PM → 2:35 PM); it must
        not invalidate the skip."""
        base = self.mod.obs_input_hash(ctx(
            screen_text="Working...\n│ claude │ 2:34 PM │ #abc12345 │"))
        tick = self.mod.obs_input_hash(ctx(
            screen_text="Working...\n│ claude │ 2:35 PM │ #abc12345 │"))
        self.assertEqual(base, tick,
                         "status bar clock change must not change the hash")

    def test_cursor_block_does_not_change_the_hash(self):
        """A cursor block character (█▋▊▉) is rendering, not content."""
        base = self.mod.obs_input_hash(ctx(screen_text="$ ls -la█"))
        tick = self.mod.obs_input_hash(ctx(screen_text="$ ls -la"))
        self.assertEqual(base, tick,
                         "cursor block change must not change the hash")

    def test_trailing_whitespace_does_not_change_the_hash(self):
        """Trailing whitespace (line-padding jitter, cursor position) must not
        invalidate the skip."""
        base = self.mod.obs_input_hash(ctx(screen_text="$ git status  \n  "))
        tick = self.mod.obs_input_hash(ctx(screen_text="$ git status\n"))
        self.assertEqual(base, tick,
                         "trailing whitespace change must not change the hash")

    def test_blank_lines_do_not_change_the_hash(self):
        """Empty lines between content must not affect the fingerprint."""
        base = self.mod.obs_input_hash(ctx(
            screen_text="line 1\n\n\nline 2"))
        tick = self.mod.obs_input_hash(ctx(
            screen_text="line 1\nline 2"))
        self.assertEqual(base, tick,
                         "blank line difference must not change the hash")

    def test_real_content_change_still_changes_the_hash(self):
        """Despite normalization, a genuine semantic change (new output line)
        MUST still produce a different hash so the Observer re-observes."""
        base = self.mod.obs_input_hash(ctx(
            screen_text="$ git status\nOn branch main"))
        changed = self.mod.obs_input_hash(ctx(
            screen_text="$ git status\nOn branch main\nnothing to commit"))
        self.assertNotEqual(base, changed,
                            "real content change must change the hash")

    def test_error_appearance_still_changes_the_hash(self):
        """An error banner appearing on screen is a semantic change that must
        produce a different hash even though the error flag is separate."""
        base = self.mod.obs_input_hash(ctx(
            screen_text="$ npm test\nAll tests passed"))
        changed = self.mod.obs_input_hash(ctx(
            screen_text="$ npm test\n⏺ API Error: rate limit exceeded"))
        self.assertNotEqual(base, changed,
                            "error banner appearance must change the hash")

    def test_tree_output_change_is_detected(self):
        """Lines with │ that are NOT status bars (tree connectors, table rows)
        must still contribute to the fingerprint — dropping them causes false
        matches on common coding agent output."""
        base = self.mod.obs_input_hash(ctx(
            screen_text="$ tree src/\n├── mod_a.py\n│   └── helper.py"))
        changed = self.mod.obs_input_hash(ctx(
            screen_text="$ tree src/\n├── mod_a.py\n│   └── helper.py\n"
                        "│   └── utils.py"))
        self.assertNotEqual(base, changed,
                            "tree output change must change the hash")

    def test_git_log_graph_change_is_detected(self):
        """git log --graph output contains │ in branch connectors; a new
        commit must change the fingerprint."""
        base = self.mod.obs_input_hash(ctx(
            screen_text="$ git log --graph\n* abc123 main\n│ * def456 branch"))
        changed = self.mod.obs_input_hash(ctx(
            screen_text="$ git log --graph\n* abc123 main\n│ * def456 branch\n"
                        "│ * 789abc branch"))
        self.assertNotEqual(base, changed,
                            "git log graph change must change the hash")

    def test_empty_screen_fingerprint(self):
        """Empty/blank screen produces a stable marker, not a crash."""
        self.assertEqual(self.mod._screen_text_fingerprint(""), "empty-screen")
        self.assertEqual(self.mod._screen_text_fingerprint("   \n  \n"),
                         "blank-screen")
        # stable across calls
        self.assertEqual(self.mod._screen_text_fingerprint(""),
                         self.mod._screen_text_fingerprint(""))


# ─── main() wiring ───────────────────────────────────────────────────────────

class SkipWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self._old_home = os.environ.get("HOME")
        self._old_argv = sys.argv
        self.mod = load_pulse(self._tmp)

    def tearDown(self):
        sys.argv = self._old_argv
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _plant_summary(self, ws_ref, verdict, obs_hash=None, carry_count=None,
                       **extra):
        d = {"ws_ref": ws_ref, "verdict": verdict, "summary": "s",
             "next": "n", "title": "t", "cwd": "/", "pr_refs": [],
             "last_updated_ts": 1, "state_hash": "x",
             "state_unchanged_since_ts": 1}
        if obs_hash is not None:
            d["obs_input_hash"] = obs_hash
        if carry_count is not None:
            d["carry_count"] = carry_count
        d.update(extra)
        p = (self._tmp / ".assistant/observer-summaries"
             / f"{ws_ref.replace(':', '_')}.json")
        p.write_text(json.dumps(d))

    def _run_pulse(self, the_ctx, observer_result=({}, {})):
        with mock.patch.object(self.mod, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t",
                                   "cwd": "/"}],
                "reuse_cached": [], "backed_off": [], "total_ws": 1,
            }), \
             mock.patch.object(self.mod, "purge_stale_awaiting"), \
             mock.patch.object(self.mod, "build_ctx",
                               return_value=dict(the_ctx)), \
             mock.patch.object(self.mod, "call_observer_batch",
                               return_value=observer_result) as obs_mock, \
             mock.patch.object(self.mod, "save_summary") as save_mock, \
             mock.patch.object(self.mod, "cmux_send") as send_mock, \
             mock.patch.object(self.mod, "pick_open_todos",
                               return_value={"bucket_b": []}), \
             mock.patch.object(self.mod, "run",
                               return_value=(0, "", "")) as run_mock:
            sys.argv = ["pulse.py"]
            rc = self.mod.main()
        return rc, obs_mock, save_mock, send_mock, run_mock

    def _metrics(self):
        p = self._tmp / ".assistant/metrics.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def _state_payload(self, run_mock):
        saved = [c for c in run_mock.call_args_list
                 if c.args and "state-write.py" in str(c.args[0])]
        return json.loads(saved[-1].kwargs.get("input_text", "{}"))

    def test_matching_hash_skips_observer_and_carries_verdict(self):
        c = ctx()
        self._plant_summary("workspace:1", "needs_user",
                            obs_hash=self.mod.obs_input_hash(c))
        rc, obs_mock, save_mock, send_mock, run_mock = self._run_pulse(c)
        self.assertEqual(rc, 0)
        obs_mock.assert_not_called()          # zero LLM calls this pulse
        send_mock.assert_not_called()         # carried verdicts never re-act
        save_mock.assert_called_once()        # ...but the LRU clock rotates
        _, carried = save_mock.call_args[0]
        self.assertEqual(carried["verdict"], "needs_user")
        self.assertEqual(carried["obs_input_hash"],
                         self.mod.obs_input_hash(c))
        payload = self._state_payload(run_mock)
        kinds = {a.get("kind") for a in payload["actions_taken"]}
        self.assertIn("skipped-no-change", kinds)
        # Metering reflects reality: no call happened.
        rec = self._metrics()[0]
        self.assertFalse(rec["observer_called"])
        self.assertEqual(rec["batch_size"], 0)
        self.assertEqual(rec["skipped"], 1)
        self.assertEqual(rec["verdicts"], {"needs_user": 1})
        self.assertEqual(rec["verdict_changes"], 0)

    def test_changed_hash_observes_normally(self):
        self._plant_summary("workspace:1", "needs_user",
                            obs_hash=self.mod.obs_input_hash(ctx()))
        changed = ctx(screen_text="brand new output")
        verdict = {"workspace:1": {"ws_ref": "workspace:1",
                                   "verdict": "active",
                                   "summary": "s", "next": "n"}}
        rc, obs_mock, save_mock, _, _ = self._run_pulse(
            changed, observer_result=(verdict, {}))
        self.assertEqual(rc, 0)
        obs_mock.assert_called_once()
        rec = self._metrics()[0]
        self.assertTrue(rec["observer_called"])
        self.assertEqual(rec["batch_size"], 1)
        self.assertEqual(rec["skipped"], 0)
        # A REAL verdict earned against the new state stores its hash for
        # the next pulse's comparison.
        _, saved = save_mock.call_args[0]
        self.assertEqual(saved["obs_input_hash"],
                         self.mod.obs_input_hash(changed))

    def test_prior_summary_without_hash_never_skips(self):
        # Pre-M2 summaries (and synthesized fallback verdicts, which are
        # saved hash-less on purpose) must be re-observed, not carried.
        self._plant_summary("workspace:1", "active", obs_hash=None)
        verdict = {"workspace:1": {"ws_ref": "workspace:1",
                                   "verdict": "active",
                                   "summary": "s", "next": "n"}}
        rc, obs_mock, _, _, _ = self._run_pulse(ctx(),
                                                observer_result=(verdict, {}))
        self.assertEqual(rc, 0)
        obs_mock.assert_called_once()

    def test_carry_increments_carry_count_in_saved_summary(self):
        c = ctx()
        self._plant_summary("workspace:1", "active",
                            obs_hash=self.mod.obs_input_hash(c),
                            carry_count=3)
        rc, obs_mock, save_mock, _, _ = self._run_pulse(c)
        self.assertEqual(rc, 0)
        obs_mock.assert_not_called()
        _, carried = save_mock.call_args[0]
        self.assertEqual(carried["carry_count"], 4)

    def test_seventh_consecutive_carry_forces_an_observation(self):
        # The structural defense against ANY hash blind spot: after
        # MAX_CONSECUTIVE_CARRIES (6) skips, an identical hash no longer
        # carries — the 7th pulse re-observes for real.
        c = ctx()
        self._plant_summary("workspace:1", "active",
                            obs_hash=self.mod.obs_input_hash(c),
                            carry_count=self.mod.MAX_CONSECUTIVE_CARRIES)
        verdict = {"workspace:1": {"ws_ref": "workspace:1",
                                   "verdict": "active",
                                   "summary": "s", "next": "n"}}
        rc, obs_mock, save_mock, _, _ = self._run_pulse(
            c, observer_result=(verdict, {}))
        self.assertEqual(rc, 0)
        obs_mock.assert_called_once()  # force-observed despite matching hash
        rec = self._metrics()[0]
        self.assertTrue(rec["observer_called"])
        self.assertEqual(rec["skipped"], 0)
        # The fresh real verdict resets the carry counter.
        _, saved = save_mock.call_args[0]
        self.assertNotIn("carry_count", saved)

    def test_carried_needs_user_reemits_its_card_without_acting(self):
        # A carried verdict must keep its human-facing card alive (cards are
        # rebuilt every pulse) with the SAME key execute_verdict would use —
        # and provably not re-send anything.
        c = ctx()
        self._plant_summary("workspace:1", "needs_user",
                            obs_hash=self.mod.obs_input_hash(c),
                            title="t", detail="please review the plan")
        rc, obs_mock, _, send_mock, run_mock = self._run_pulse(c)
        self.assertEqual(rc, 0)
        obs_mock.assert_not_called()
        send_mock.assert_not_called()          # nudge/cleanup never re-fire
        payload = self._state_payload(run_mock)
        cards = {a["key"] for a in payload["awaiting_input"]}
        self.assertIn("workspace:1:needs_user", cards)  # execute_verdict's key

    def test_carried_ready_for_cleanup_reemits_card_never_sends(self):
        # ready_for_cleanup without an Assistant-merge record downgrades to a
        # confirm card; on carry that card must re-emit, and /cleanup must
        # provably NOT be sent (nor any merge dispatched).
        c = ctx()
        self._plant_summary("workspace:1", "ready_for_cleanup",
                            obs_hash=self.mod.obs_input_hash(c))
        with mock.patch.object(self.mod, "run_merge_pr_dispatch") as merge_mock:
            rc, obs_mock, _, send_mock, run_mock = self._run_pulse(c)
        self.assertEqual(rc, 0)
        obs_mock.assert_not_called()
        send_mock.assert_not_called()          # /cleanup never re-fires
        merge_mock.assert_not_called()         # merge never re-fires
        payload = self._state_payload(run_mock)
        cards = {a["key"] for a in payload["awaiting_input"]}
        self.assertIn("workspace:1:cleanup-needs-confirm", cards)

    def test_carried_ready_for_merge_never_redispatches(self):
        c = ctx()
        self._plant_summary("workspace:1", "ready_for_merge",
                            obs_hash=self.mod.obs_input_hash(c),
                            pr_refs=[123])
        with mock.patch.object(self.mod, "run_merge_pr_dispatch") as merge_mock:
            rc, obs_mock, _, send_mock, _ = self._run_pulse(c)
        self.assertEqual(rc, 0)
        obs_mock.assert_not_called()
        merge_mock.assert_not_called()
        send_mock.assert_not_called()

    def test_observer_failure_saves_synth_without_hash(self):
        # Batch fails → synthesized 'active' is saved WITHOUT a hash so the
        # next pulse retries instead of carrying the failure forward.
        rc, _, save_mock, _, _ = self._run_pulse(ctx(),
                                                 observer_result=({}, {}))
        self.assertEqual(rc, 0)
        _, synth = save_mock.call_args[0]
        self.assertEqual(synth["verdict"], "active")
        self.assertNotIn("obs_input_hash", synth)


# ─── eval fixture replay: zero divergence ────────────────────────────────────

class FixtureReplayTests(unittest.TestCase):
    """Replay every evals/observer fixture through the skip path: pulse N
    earned `expected.json`'s verdict against the fixture's state; pulse N+1
    sees byte-identical state. The skip must engage on every fixture and the
    carried verdict must equal the earned one — zero divergence. Then mutate
    the state and require the skip to DISENGAGE (a changed workspace can
    never be judged by a stale verdict)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp_obj = TemporaryDirectory()
        cls.home = fixture_home(Path(cls._tmp_obj.name))
        cls._old_home = os.environ.get("HOME")
        cls.mod = load_pulse(cls.home)
        cls.fixture_dirs = sorted(
            d for d in FIXTURES.iterdir()
            if d.is_dir() and (d / "ctx.json").exists())

    @classmethod
    def tearDownClass(cls):
        if cls._old_home is not None:
            os.environ["HOME"] = cls._old_home
        cls._tmp_obj.cleanup()

    def _fixture_ctx(self, d: Path) -> dict:
        c = json.loads((d / "ctx.json").read_text())
        t = d / "transcript.jsonl"
        c["transcript_path"] = str(t) if t.exists() else None
        return c

    def test_fixture_suite_is_present(self):
        self.assertGreaterEqual(len(self.fixture_dirs), 13)

    def test_zero_verdict_divergence_with_skip_enabled(self):
        divergences = []
        for d in self.fixture_dirs:
            c = self._fixture_ctx(d)
            expected = json.loads((d / "expected.json").read_text())["verdict"]
            earned_hash = self.mod.obs_input_hash(c)
            prior_summary = {  # what pulse N left on disk
                "verdict": expected, "summary": "s", "next": "n",
                "obs_input_hash": earned_hash,
                "ws_ref": c.get("ws_ref"), "title": c.get("title"),
                "cwd": c.get("cwd"), "pr_refs": [], "last_updated_ts": 1,
                "state_hash": "x", "state_unchanged_since_ts": 1,
            }
            # Pulse N+1, unchanged state:
            rehash = self.mod.obs_input_hash(self._fixture_ctx(d))
            if rehash != earned_hash:
                divergences.append((d.name, "skip failed to engage"))
                continue
            carried = self.mod.carry_forward_verdict(prior_summary)
            if carried.get("verdict") != expected:
                divergences.append((d.name, f"carried {carried.get('verdict')}"
                                            f" != {expected}"))
        self.assertEqual(divergences, [])

    def test_skip_disengages_on_any_state_change(self):
        for d in self.fixture_dirs:
            c = self._fixture_ctx(d)
            before = self.mod.obs_input_hash(c)
            # Screen change.
            changed = dict(c, screen_text=(c.get("screen_text") or "") + "\n$ ")
            self.assertNotEqual(self.mod.obs_input_hash(changed), before,
                                msg=f"{d.name}: screen change missed")
            # Transcript growth (on a copy — never touch the fixture).
            if c.get("transcript_path"):
                tmp_t = self.home / f"copy-{d.name}.jsonl"
                shutil.copyfile(c["transcript_path"], tmp_t)
                grown = dict(c, transcript_path=str(tmp_t))
                base = self.mod.obs_input_hash(grown)
                with open(tmp_t, "a") as f:
                    f.write('{"type": "assistant", "message": {}}\n')
                self.assertNotEqual(self.mod.obs_input_hash(grown), base,
                                    msg=f"{d.name}: transcript growth missed")


# ─── synthetic-day replay: >=30% fewer Observer calls ────────────────────────

class CallReductionReplayTests(unittest.TestCase):
    """A synthetic day where most pulses have no state change: 6 workspaces
    observed over 48 pulses (5-min cadence ≈ 4 hours of a real day), each
    workspace's state changing only every 8th pulse. Replays the exact skip
    predicate main() uses (stored obs_input_hash comparison) and requires
    >=30% reduction in BOTH per-workspace observations and pulses that spawn
    any Observer subprocess at all."""

    N_WS = 6
    N_PULSES = 48
    CHANGE_EVERY = 8

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = fixture_home(Path(self._tmp_obj.name))
        self._old_home = os.environ.get("HOME")
        self.mod = load_pulse(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self._tmp_obj.cleanup()

    def _ctx_at(self, ws: int, pulse: int) -> dict:
        # State only moves when pulse//CHANGE_EVERY ticks over; the age
        # counter grows EVERY pulse (as it does in production) to prove it
        # can't defeat the skip.
        return ctx(ws_ref=f"workspace:{ws}",
                   screen_text=f"ws{ws} epoch {pulse // self.CHANGE_EVERY}",
                   last_turn_age_sec=pulse * 300)

    def test_reduction_of_at_least_30_percent(self):
        # main()'s exact predicate: carry iff the stored hash matches AND the
        # verdict hasn't already been carried MAX_CONSECUTIVE_CARRIES times.
        # With the idle-age band in the hash and the carry cap, the reduction
        # drops from the pre-review 87.5% to 75% — still far above the 30%
        # bar, and now threshold crossings + hash blind spots re-observe.
        store: dict = {}   # ws → hash stored with the last verdict
        carries: dict = {}  # ws → consecutive carries since last observation
        observed_with_skip = 0
        pulses_with_call = 0
        gaps: list[int] = []  # consecutive un-observed pulses, per ws stretch
        for pulse in range(self.N_PULSES):
            batch = []
            for ws in range(self.N_WS):
                h = self.mod.obs_input_hash(self._ctx_at(ws, pulse))
                if store.get(ws) == h and \
                        carries.get(ws, 0) < self.mod.MAX_CONSECUTIVE_CARRIES:
                    carries[ws] = carries.get(ws, 0) + 1
                    continue  # carried forward
                batch.append(ws)
                gaps.append(carries.get(ws, 0))
                carries[ws] = 0
                store[ws] = h  # verdict earned against h, hash saved
            observed_with_skip += len(batch)
            if batch:
                pulses_with_call += 1

        observed_without = self.N_WS * self.N_PULSES
        ws_reduction = 1 - observed_with_skip / observed_without
        call_reduction = 1 - pulses_with_call / self.N_PULSES
        self.assertGreaterEqual(
            ws_reduction, 0.30,
            msg=f"per-ws observations only fell {ws_reduction:.0%}")
        self.assertGreaterEqual(
            call_reduction, 0.30,
            msg=f"observer-call pulses only fell {call_reduction:.0%}")
        # The recomputed number itself, pinned so a hash/cap change shows up
        # here: 12 observations per ws over 48 pulses = 75% reduction (6
        # screen epochs + the 1800s band crossing + one cap-forced observe
        # per stretch of unchanged pulses).
        self.assertAlmostEqual(ws_reduction, 0.75)
        self.assertAlmostEqual(call_reduction, 0.75)
        # And the cap held everywhere: no workspace ever went more than
        # MAX_CONSECUTIVE_CARRIES pulses without a real observation.
        self.assertTrue(all(g <= self.mod.MAX_CONSECUTIVE_CARRIES
                            for g in gaps + list(carries.values())))


if __name__ == "__main__":
    unittest.main()
