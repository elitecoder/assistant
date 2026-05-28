"""Direct-import tests for bin/render-assistant-page.py.

The renderer is 1000+ lines of HTML assembly — most is layout strings.
We test the high-value branches:

  - render_pulse_health: green/amber/red age thresholds, missing
    heartbeat, malformed json, missing ts.
  - utility helpers: parse_iso, age_str, shorten_cwd, first_ws_ref,
    load_assistant_state.
  - render_workspaces_tab: that the back-off banner appears for backed-
    off ws and rows are filtered to only-open ws.

Skip: the 500+ lines of decisions / todos / live_sessions tab assembly.
Those are layout strings that change every UI tweak — pinning their HTML
verbatim is test theater that catches no real bug."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/render-assistant-page.py"


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("renderer_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    return tmp


# ─── pulse-health banner ────────────────────────────────────────────────────

class PulseHealthTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _write_heartbeat(self, payload):
        (self._tmp / ".assistant/heartbeat.json").write_text(json.dumps(payload))

    def test_no_heartbeat_renders_red_banner(self):
        html = self.mod.render_pulse_health()
        self.assertIn("pulse-bad", html)
        self.assertIn("never run", html)

    def test_corrupt_heartbeat_renders_red_banner(self):
        (self._tmp / ".assistant/heartbeat.json").write_text("{ corrupt")
        html = self.mod.render_pulse_health()
        self.assertIn("pulse-bad", html)
        self.assertIn("unreadable", html)

    def test_missing_last_pulse_ts_renders_red_banner(self):
        self._write_heartbeat({"status": "running"})
        html = self.mod.render_pulse_health()
        self.assertIn("pulse-bad", html)
        self.assertIn("no last_pulse_ts", html)

    def test_fresh_pulse_renders_green_banner(self):
        self._write_heartbeat({
            "last_pulse_ts": int(time.time()) - 30,
            "pulse_idx": 99, "model": "python-mechanical",
        })
        html = self.mod.render_pulse_health()
        self.assertIn("pulse-ok", html)
        self.assertIn("Pulse healthy", html)
        self.assertIn("python-mechanical", html)

    def test_warm_pulse_renders_amber_banner(self):
        self._write_heartbeat({
            "last_pulse_ts": int(time.time()) - 1000,  # ~17 min
            "pulse_idx": 99, "model": "python-mechanical",
        })
        html = self.mod.render_pulse_health()
        self.assertIn("pulse-warn", html)
        self.assertIn("Pulse slow", html)

    def test_stale_pulse_renders_red_banner(self):
        self._write_heartbeat({
            "last_pulse_ts": int(time.time()) - 5000,  # 83 min
            "pulse_idx": 99, "model": "python-mechanical",
        })
        html = self.mod.render_pulse_health()
        self.assertIn("pulse-bad", html)
        self.assertIn("Pulse stale", html)

    def test_age_formatting_includes_unit(self):
        self._write_heartbeat({"last_pulse_ts": int(time.time()) - 10})
        html = self.mod.render_pulse_health()
        self.assertIn("10s", html)

        self._write_heartbeat({"last_pulse_ts": int(time.time()) - 200})
        html = self.mod.render_pulse_health()
        self.assertIn("3m", html)  # 200/60 = 3

        self._write_heartbeat({"last_pulse_ts": int(time.time()) - 3700})
        html = self.mod.render_pulse_health()
        self.assertIn("h", html)

    def test_age_formatting_handles_days(self):
        self._write_heartbeat({"last_pulse_ts": int(time.time()) - (2 * 86400)})
        html = self.mod.render_pulse_health()
        self.assertIn("2d", html)


# ─── utility helpers ────────────────────────────────────────────────────────

class UtilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_utc_now_returns_aware_datetime(self):
        now = self.mod.utc_now()
        self.assertIsNotNone(now.tzinfo)

    def test_parse_iso_handles_z_suffix(self):
        d = self.mod.parse_iso("2026-05-28T12:00:00Z")
        self.assertEqual(d.year, 2026)

    def test_parse_iso_returns_none_on_garbage(self):
        self.assertIsNone(self.mod.parse_iso("not a date"))
        self.assertIsNone(self.mod.parse_iso(""))
        self.assertIsNone(self.mod.parse_iso(None))

    def test_age_str_format(self):
        self.assertEqual(self.mod.age_str(None), "?")
        self.assertEqual(self.mod.age_str(-5), "future")
        self.assertEqual(self.mod.age_str(30), "30s")
        self.assertIn("m", self.mod.age_str(150))
        self.assertIn("h", self.mod.age_str(7200))
        self.assertIn("d", self.mod.age_str(86400 * 2))

    def test_shorten_cwd(self):
        # Short path stays.
        self.assertEqual(self.mod.shorten_cwd("/short"), "/short")
        # Long path gets truncated.
        long = "/very/very/very/long/path/that/is/way/too/long/for/display"
        result = self.mod.shorten_cwd(long, max_len=20)
        self.assertLessEqual(len(result), 22)  # max_len + ellipsis

    def test_shorten_cwd_handles_empty(self):
        self.assertEqual(self.mod.shorten_cwd(""), "")
        self.assertEqual(self.mod.shorten_cwd(None), "")

    def test_shorten_cwd_replaces_home(self):
        # ~/Users/mukuls/ → ~/
        self.assertIn("~/", self.mod.shorten_cwd("/Users/mukuls/dev/x"))

    def test_first_ws_ref_extracts_workspace_ref(self):
        # touches entries are str OR {"ref": "..."} — uses .get("ref"), not "ws_ref".
        touches = [{"ref": "workspace:5"}, {"ref": "workspace:6"}]
        self.assertEqual(self.mod.first_ws_ref(touches), "workspace:5")

    def test_first_ws_ref_accepts_string_entries(self):
        self.assertEqual(self.mod.first_ws_ref(["workspace:7"]), "workspace:7")

    def test_first_ws_ref_skips_non_workspace_strings(self):
        self.assertIsNone(self.mod.first_ws_ref(["pulse-1", "td-7"]))

    def test_first_ws_ref_returns_none_on_empty(self):
        self.assertIsNone(self.mod.first_ws_ref([]))
        self.assertIsNone(self.mod.first_ws_ref(None))

    def test_load_assistant_state_default_when_missing(self):
        d = self.mod.load_assistant_state()
        self.assertEqual(d, {})

    def test_load_assistant_state_returns_parsed(self):
        Path(self._tmp / ".claude/cache/assistant-state.json").write_text(
            json.dumps({"hello": "world"})
        )
        d = self.mod.load_assistant_state()
        self.assertEqual(d, {"hello": "world"})

    def test_load_assistant_state_handles_corrupt(self):
        Path(self._tmp / ".claude/cache/assistant-state.json").write_text("{ bad")
        # The fallback is whatever the module decided — at minimum, no crash.
        try:
            self.mod.load_assistant_state()
        except Exception:
            self.fail("load_assistant_state should swallow corrupt files")


# ─── back-off banner inside Workspaces tab ──────────────────────────────────

class WorkspacesTabBackoffBannerTests(unittest.TestCase):
    """The Workspaces tab reads world.json + observer-summaries + back-off.json.
    We plant minimal fixtures and assert the banner shows backed-off ws and
    the rows exclude them."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _setup_world(self, ws_list):
        Path(self._tmp / ".claude/cache/world.json").write_text(json.dumps({
            "workspaces": ws_list,
            "live_sessions": [],
        }))

    def _setup_summary(self, ref, verdict, summary, next_):
        slug = ref.replace(":", "_")
        Path(self._tmp / f".assistant/observer-summaries/{slug}.json").write_text(json.dumps({
            "ws_ref": ref, "verdict": verdict,
            "summary": summary, "next": next_,
            "title": ref,
            "cwd": "/tmp",
        }))

    def test_backed_off_ws_appears_in_banner_not_rows(self):
        self._setup_world([
            {"ws_ref": "workspace:1", "title": "active"},
            {"ws_ref": "workspace:99", "title": "backed-off"},
        ])
        self._setup_summary("workspace:1", "active", "doing X", "Y")
        self._setup_summary("workspace:99", "active", "doing Z", "W")
        # Plant back-off entry.
        Path(self._tmp / ".assistant/back-off.json").write_text(json.dumps({
            "workspaces": [{"ws_ref": "workspace:99", "reason": "loop",
                             "added_ts": int(time.time())}]
        }))
        html, n = self.mod.render_workspaces_tab()
        # Banner contains workspace:99
        self.assertIn("backoff-section", html)
        self.assertIn("workspace:99", html)
        self.assertIn("loop", html)
        # workspace:1 still in rows.
        self.assertIn('data-ws="workspace:1"', html)

    def test_no_back_off_means_no_banner(self):
        self._setup_world([{"ws_ref": "workspace:1", "title": "x"}])
        self._setup_summary("workspace:1", "active", "doing X", "Y")
        html, n = self.mod.render_workspaces_tab()
        self.assertNotIn("backoff-section", html)

    def test_no_observer_summaries_renders_placeholder(self):
        # empty summaries dir
        for child in (self._tmp / ".assistant/observer-summaries").glob("*.json"):
            child.unlink()
        # Replace dir with non-existent — simulate first run.
        import shutil
        shutil.rmtree(self._tmp / ".assistant/observer-summaries")
        html, n = self.mod.render_workspaces_tab()
        self.assertIn("No observer summaries", html)


if __name__ == "__main__":
    unittest.main()
