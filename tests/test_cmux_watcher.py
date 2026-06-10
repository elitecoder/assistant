"""Tests for bin/cmux-watcher.py + bin/tools/pattern-feedback.py +
lesson-extractor pattern feedback/discovery.

Loaded by file path (the scripts are hyphenated CLIs, not importable modules).
Everything is exercised against a tmp HOME so no real inbox / pattern bank is
touched, and cmux is never shelled out to — screen reads are injected."""
from __future__ import annotations

import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def load_module(name: str, rel: str, env: dict | None = None):
    """Import a hyphenated bin script as a module, with env applied first so its
    module-level path constants resolve under the tmp HOME."""
    if env:
        os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── cmux-watcher: pattern matching + inbox drop ──────────────────────────────

class TestPatternMatching(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.assistant = self.home / ".assistant"
        self.assistant.mkdir(parents=True)
        self.bank_path = self.assistant / "pattern_bank.json"
        self.env = {
            "HOME": str(self.home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(self.assistant),
            "CMUX_PATTERN_BANK": str(self.bank_path),
        }
        self.mod = load_module("cmux_watcher_pm", "bin/cmux-watcher.py", self.env)

    def tearDown(self):
        self._tmp.cleanup()

    def _bank(self):
        return self.mod.PatternBank(self.bank_path)

    def test_default_bank_created_on_first_load(self):
        self.assertFalse(self.bank_path.exists())
        bank = self._bank()
        self.assertTrue(self.bank_path.exists())
        self.assertTrue(len(bank.patterns) >= 8)

    def test_pattern_match_pr_opened(self):
        bank = self._bank()
        hits = bank.match("...\nPR #123 opened against main\n...")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["id"], "pr-opened")
        self.assertEqual(hits[0]["signal"], "work_complete")

    def test_pattern_match_awaiting(self):
        bank = self._bank()
        hits = bank.match("the change is awaiting your review now")
        self.assertTrue(hits)
        # awaiting-review is high priority and signals needs_input.
        self.assertEqual(hits[0]["signal"], "needs_input")

    def test_pattern_muted_never_matches(self):
        # Write a bank with a single muted pattern; it must not match.
        self.bank_path.write_text(json.dumps({
            "version": 1,
            "patterns": [
                {"id": "noisy", "regex": "build done", "signal": "work_complete",
                 "priority": "muted"},
            ],
        }))
        bank = self._bank()
        self.assertEqual(bank.match("build done in 4s"), [])

    def test_priority_ordering(self):
        self.bank_path.write_text(json.dumps({
            "version": 1,
            "patterns": [
                {"id": "lowp", "regex": "thing", "signal": "work_complete", "priority": "low"},
                {"id": "highp", "regex": "thing", "signal": "needs_input", "priority": "high"},
            ],
        }))
        bank = self._bank()
        hits = bank.match("a thing happened")
        self.assertEqual(hits[0]["id"], "highp")  # high sorts first


class TestInboxDrop(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.assistant = self.home / ".assistant"
        self.assistant.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(self.assistant),
            "CMUX_PATTERN_BANK": str(self.assistant / "pattern_bank.json"),
        }
        self.mod = load_module("cmux_watcher_drop", "bin/cmux-watcher.py", self.env)
        self.inbox = self.assistant / "inbox"

    def tearDown(self):
        self._tmp.cleanup()

    def test_inbox_drop_atomic_and_shape(self):
        # No .tmp left behind, file parses, carries the documented fields.
        path = self.mod.drop_inbox_item(
            "workspace:42", "needs_input", "awaiting-review",
            "last line of screen", inbox_dir=self.inbox)
        self.assertTrue(path.exists())
        leftovers = list(self.inbox.glob(".*tmp"))
        self.assertEqual(leftovers, [], "atomic write left a temp file behind")
        item = json.loads(path.read_text())
        self.assertEqual(item["event"], "workspace_signal")
        self.assertEqual(item["ws_ref"], "workspace:42")
        self.assertEqual(item["signal_type"], "needs_input")
        self.assertEqual(item["pattern_matched"], "awaiting-review")
        self.assertIn("ts", item)
        self.assertIn("screen_snippet", item)

    def test_inbox_filename_prefix(self):
        path = self.mod.drop_inbox_item(
            "workspace:7", "work_complete", "pr-opened", "x", inbox_dir=self.inbox)
        # Inbox signals are cmux-*.json (NOT pulse-*.json).
        self.assertTrue(path.name.startswith("cmux-"))
        self.assertTrue(path.name.endswith(".json"))


# ─── cmux-watcher: event classification + end-to-end handling ─────────────────

def _evt(name, *, request_id="r1", workspace_id="UUID-1", cwd="/x", phase="completed"):
    return {
        "type": "event",
        "name": name,
        "workspace_id": workspace_id,
        "payload": {
            "_opencode_request_id": request_id,
            "workspace_id": workspace_id,
            "cwd": cwd,
            "phase": phase,
        },
    }


class TestEventHandling(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.assistant = self.home / ".assistant"
        self.assistant.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(self.assistant),
            "CMUX_PATTERN_BANK": str(self.assistant / "pattern_bank.json"),
        }
        self.mod = load_module("cmux_watcher_evt", "bin/cmux-watcher.py", self.env)
        self.inbox = self.assistant / "inbox"

    def tearDown(self):
        self._tmp.cleanup()

    def _components(self):
        bank = self.mod.PatternBank(self.assistant / "pattern_bank.json")
        state = self.mod.WatcherState(cooldown_sec=0)
        # Resolver that never shells out: identity map.
        resolver = mock.Mock()
        resolver.resolve = lambda u: ("workspace:99" if u else None)
        return bank, state, resolver

    def test_ack_and_heartbeat_ignored(self):
        self.assertIsNone(self.mod.classify_event({"type": "ack"}))
        self.assertIsNone(self.mod.classify_event({"type": "heartbeat"}))

    def test_pretooluse_ignored(self):
        self.assertIsNone(self.mod.classify_event(_evt("agent.hook.PreToolUse")))

    def test_needs_input_always_drops(self):
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.AskUserQuestion"), bank, state, resolver,
            screen_reader=lambda ws: "Which option?\n> 1. yes")
        self.assertIsNotNone(res)
        self.assertEqual(res["signal_type"], "needs_input")
        self.assertEqual(res["pattern_matched"], "AskUserQuestion")

    def test_notification_drops_needs_input(self):
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="rN"), bank, state, resolver,
            screen_reader=lambda ws: "waiting")
        self.assertEqual(res["signal_type"], "needs_input")

    def test_turn_end_with_pattern_drops(self):
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rS"), bank, state, resolver,
            screen_reader=lambda ws: "Done. PR #321 opened for review.")
        self.assertIsNotNone(res)
        self.assertEqual(res["pattern_matched"], "pr-opened")
        self.assertEqual(res["signal_type"], "work_complete")

    def test_turn_end_no_pattern_is_silent(self):
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rQuiet"), bank, state, resolver,
            screen_reader=lambda ws: "nothing notable here, just chatter")
        self.assertIsNone(res, "a plain turn-end with no pattern must not drop")

    def test_turn_end_dead_workspace_silent(self):
        bank, state, resolver = self._components()
        # read-screen returns "" for a dead/headless workspace.
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rDead"), bank, state, resolver,
            screen_reader=lambda ws: "")
        self.assertIsNone(res)

    def test_request_id_dedup(self):
        bank, state, resolver = self._components()
        evt = _evt("agent.hook.Stop", request_id="dup1")
        first = self.mod.handle_event(
            evt, bank, state, resolver,
            screen_reader=lambda ws: "PR #1 opened")
        # Same request id (received→completed pair) must not double-drop.
        second = self.mod.handle_event(
            evt, bank, state, resolver,
            screen_reader=lambda ws: "PR #1 opened")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_cooldown_suppresses_repeat(self):
        bank = self.mod.PatternBank(self.assistant / "pattern_bank.json")
        state = self.mod.WatcherState(cooldown_sec=3600)  # long cooldown
        resolver = mock.Mock()
        resolver.resolve = lambda u: "workspace:5"
        a = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="c1"), bank, state, resolver,
            screen_reader=lambda ws: "x")
        b = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="c2"), bank, state, resolver,
            screen_reader=lambda ws: "x")
        self.assertIsNotNone(a)
        self.assertIsNone(b, "second needs_input within cooldown must be suppressed")

    def test_malformed_event_no_crash(self):
        bank, state, resolver = self._components()
        for bad in (None, {}, {"type": "event"}, {"type": "event", "name": 5},
                    {"type": "event", "name": "agent.hook.Stop", "payload": "notadict"}):
            # Should never raise; returns None or handles gracefully.
            try:
                self.mod.handle_event(bad, bank, state, resolver,
                                      screen_reader=lambda ws: "")
            except Exception as e:  # noqa: BLE001
                self.fail(f"handle_event raised on {bad!r}: {e}")


# ─── pattern hot-reload ────────────────────────────────────────────────────────

class TestPatternHotReload(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.assistant = self.home / ".assistant"
        self.assistant.mkdir(parents=True)
        self.bank_path = self.assistant / "pattern_bank.json"
        self.env = {
            "HOME": str(self.home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(self.assistant),
            "CMUX_PATTERN_BANK": str(self.bank_path),
        }
        self.mod = load_module("cmux_watcher_hot", "bin/cmux-watcher.py", self.env)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pattern_bank_hotreload(self):
        self.bank_path.write_text(json.dumps({
            "version": 1,
            "patterns": [{"id": "a", "regex": "alpha", "signal": "work_complete",
                          "priority": "low"}],
        }))
        bank = self.mod.PatternBank(self.bank_path)
        self.assertTrue(bank.match("alpha here"))
        self.assertEqual(bank.match("beta here"), [])

        # Rewrite the bank with a NEW pattern + a clearly newer mtime.
        future = time.time() + 10
        self.bank_path.write_text(json.dumps({
            "version": 2,
            "patterns": [{"id": "b", "regex": "beta", "signal": "needs_input",
                          "priority": "high"}],
        }))
        os.utime(self.bank_path, (future, future))

        # match() calls maybe_reload() first — the new pattern takes effect.
        hits = bank.match("beta here")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["id"], "b")


# ─── pattern-feedback CLI ───────────────────────────────────────────────────────

class TestPatternFeedback(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.bank_path = self.home / "pattern_bank.json"
        self.env = {"HOME": str(self.home), "CMUX_PATTERN_BANK": str(self.bank_path)}
        self.mod = load_module("pattern_feedback_mod", "bin/tools/pattern-feedback.py", self.env)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, patterns):
        self.bank_path.write_text(json.dumps({"version": 1, "patterns": patterns}))

    def test_apply_feedback_relevant_boosts(self):
        p = {"id": "x", "regex": "a", "signal": "work_complete", "priority": "low"}
        out = self.mod.apply_feedback(dict(p), "relevant")
        self.assertEqual(out["priority"], "medium")
        self.assertEqual(out["hit_count"], 1)

    def test_apply_feedback_relevant_caps_at_high(self):
        p = {"id": "x", "priority": "high"}
        out = self.mod.apply_feedback(dict(p), "relevant")
        self.assertEqual(out["priority"], "high")

    def test_pattern_feedback_noise_mutes_after_threshold(self):
        # hit=0, so the first noise vote (noise=1 > 0*2) immediately mutes.
        self._write([{"id": "n", "regex": "z", "signal": "work_complete",
                      "priority": "medium"}])
        rc = self.mod.main(["--pattern-id", "n", "--feedback", "noise",
                            "--bank", str(self.bank_path)])
        self.assertEqual(rc, 0)
        data = json.loads(self.bank_path.read_text())
        pat = data["patterns"][0]
        self.assertEqual(pat["priority"], "muted")
        self.assertEqual(pat["noise_count"], 1)

    def test_noise_does_not_mute_when_hits_dominate(self):
        # hit=5 → need noise > 10 before muting; one noise vote keeps priority.
        self._write([{"id": "n", "regex": "z", "signal": "work_complete",
                      "priority": "high", "hit_count": 5, "noise_count": 0}])
        self.mod.main(["--pattern-id", "n", "--feedback", "noise",
                       "--bank", str(self.bank_path)])
        pat = json.loads(self.bank_path.read_text())["patterns"][0]
        self.assertEqual(pat["priority"], "high")
        self.assertEqual(pat["noise_count"], 1)

    def test_unknown_pattern_id_returns_3(self):
        self._write([{"id": "n", "regex": "z", "signal": "x", "priority": "low"}])
        rc = self.mod.main(["--pattern-id", "nope", "--feedback", "noise",
                            "--bank", str(self.bank_path)])
        self.assertEqual(rc, 3)

    def test_atomic_write_no_tmp_left(self):
        self._write([{"id": "n", "regex": "z", "signal": "x", "priority": "low"}])
        self.mod.main(["--pattern-id", "n", "--feedback", "relevant",
                       "--bank", str(self.bank_path)])
        self.assertEqual(list(self.home.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
