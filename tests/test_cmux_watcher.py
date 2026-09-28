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

    def _write_june_bank(self, ci_green_extra=None):
        """The on-disk bank written before ci-green gained `suppress`."""
        ci_green = {"id": "ci-green", "regex": r"CI (is )?green",
                    "signal": "work_complete", "priority": "medium",
                    **(ci_green_extra or {})}
        self.bank_path.write_text(json.dumps({"version": 1, "patterns": [
            ci_green,
            {"id": "pr-opened", "regex": r"PR #\d+ opened",
             "signal": "work_complete", "priority": "high"},
        ]}))

    def test_old_bank_inherits_default_suppress(self):
        self._write_june_bank()
        before = self.bank_path.read_text()
        bank = self._bank()
        by_id = {p["id"]: p for p in bank.patterns}
        self.assertIs(by_id["ci-green"]["suppress"], True)
        # pr-opened has no suppress in the defaults, so nothing is invented.
        self.assertNotIn("suppress", by_id["pr-opened"])
        self.assertEqual(self.bank_path.read_text(), before, "the user's file must not be rewritten")

    def test_old_bank_ci_green_turn_end_is_silent(self):
        self._write_june_bank()
        bank = self._bank()
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rCI"), bank,
            self.mod.WatcherState(cooldown_sec=0), FakeResolver(),
            screen_reader=lambda ws: "All done — CI is green",
            session_reader=SessionReaderSpy())
        self.assertIsNone(res, "a suppressed default must stay quiet in an old bank")

    def test_noisy_needs_input_word_lists_are_muted_by_default(self):
        """Even an old on-disk bank without `suppress` keys stays quiet for
        stranded / awaiting-review / emit-card, which fired on status prose."""
        self.bank_path.write_text(json.dumps({"version": 1, "patterns": [
            {"id": pid, "regex": rx, "signal": "needs_input", "priority": "high"}
            for pid, rx in (("stranded", "blocked"), ("awaiting-review", "awaiting.{0,30}review"),
                            ("emit-card", "needs_user"))]}))
        bank = self._bank()
        for text in ("the rollout is blocked on infra",
                     "Awaiting the standing adversary, code-review, and G3",
                     "card state needs_user"):
            res = self.mod.handle_event(
                _evt("agent.hook.Stop", request_id=text), bank,
                self.mod.WatcherState(cooldown_sec=0), FakeResolver(),
                screen_reader=lambda ws: "", session_reader=SessionReaderSpy(last_text=text))
            self.assertIsNone(res, text)

    def test_a_suppressed_hit_doesnt_hide_a_real_one(self):
        self.bank_path.write_text(json.dumps({"version": 1, "patterns": [
            {"id": "awaiting-review", "regex": "awaiting review", "signal": "needs_input",
             "priority": "high", "suppress": True},
            {"id": "ci-red", "regex": "CI is red", "signal": "needs_input", "priority": "medium"}]}))
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="both"), self._bank(),
            self.mod.WatcherState(cooldown_sec=0), FakeResolver(),
            screen_reader=lambda ws: "",
            session_reader=SessionReaderSpy(last_text="PR awaiting review, but CI is red"))
        self.assertEqual(res["pattern_matched"], "ci-red")

    def test_explicit_suppress_in_file_wins(self):
        self._write_june_bank({"suppress": False})
        by_id = {p["id"]: p for p in self._bank().patterns}
        self.assertIs(by_id["ci-green"]["suppress"], False)


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
        self.assertNotIn("ws_title", item, "unresolved title must be omitted, not null")
        self.assertNotIn("last_message", item)

    def test_inbox_drop_carries_title_and_last_message(self):
        path = self.mod.drop_inbox_item(
            "workspace:244", "needs_input", "AskUserQuestion", "snippet",
            inbox_dir=self.inbox, ws_title="Green E2E Suite",
            last_message="Should I rebase or merge main?")
        item = json.loads(path.read_text())
        self.assertEqual(item["ws_title"], "Green E2E Suite")
        self.assertEqual(item["last_message"], "Should I rebase or merge main?")
        self.assertEqual(item["screen_snippet"], "snippet")

    def test_inbox_filename_prefix(self):
        path = self.mod.drop_inbox_item(
            "workspace:7", "work_complete", "pr-opened", "x", inbox_dir=self.inbox)
        # Inbox signals are cmux-*.json (NOT pulse-*.json).
        self.assertTrue(path.name.startswith("cmux-"))
        self.assertTrue(path.name.endswith(".json"))


# ─── cmux-watcher: event classification + end-to-end handling ─────────────────

def _evt(name, *, request_id="r1", workspace_id="UUID-1", cwd="/x", phase="completed",
         session_id="sess-1"):
    return {
        "type": "event",
        "name": name,
        "workspace_id": workspace_id,
        "payload": {
            "_opencode_request_id": request_id,
            "workspace_id": workspace_id,
            "cwd": cwd,
            "session_id": session_id,
            "phase": phase,
        },
    }


class FakeResolver:
    """Never shells out: every UUID resolves to one ref and (optionally) a title."""

    def __init__(self, ref="workspace:99", title=None):
        self.ref = ref
        self._title = title

    def resolve(self, uuid):
        return self.ref if uuid else None

    def title(self, uuid):
        return self._title if uuid else None


class SessionReaderSpy:
    """Stands in for read_session; records each call's arguments."""

    def __init__(self, question=None, last_text=None, title=None, unreadable=False,
                 pending_tool=None, api_error=False):
        self.reply = None if unreadable else {
            "question": question, "last_text": last_text, "title": title,
            "pending_tool": pending_tool, "api_error": api_error}
        self.calls = []

    def __call__(self, cwd, session_id):
        self.calls.append((cwd, session_id))
        return self.reply


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

    def _components(self, title=None):
        bank = self.mod.PatternBank(self.assistant / "pattern_bank.json")
        state = self.mod.WatcherState(cooldown_sec=0)
        return bank, state, FakeResolver(title=title)

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

    def test_classify_passes_session_id_through(self):
        cls = self.mod.classify_event(_evt("agent.hook.Stop", session_id="S-42"))
        self.assertEqual(cls["session_id"], "S-42")

    def test_ask_user_question_reads_pending_question(self):
        bank, state, resolver = self._components(title="Fix archself deferral door")
        reader = SessionReaderSpy(question="Should I rebase or merge main?",
                                  last_text="Two ways to land this.")
        res = self.mod.handle_event(
            _evt("agent.hook.AskUserQuestion", cwd="/w/repo", session_id="S-1"),
            bank, state, resolver, screen_reader=lambda ws: "Which option?",
            session_reader=reader)
        self.assertEqual(reader.calls, [("/w/repo", "S-1")])
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["ws_title"], "Fix archself deferral door")
        self.assertEqual(item["last_message"], "Should I rebase or merge main?")

    def test_notification_pings_when_the_agent_asks_something(self):
        bank, state, resolver = self._components()
        reader = SessionReaderSpy(question="an old question",
                                  last_text="Fixed it.\n\nWant me to open the PR?",
                                  title="assistant (fix/comms)")
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="rN2", session_id="S-2"),
            bank, state, resolver, screen_reader=lambda ws: "waiting",
            session_reader=reader)
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["last_message"], "Fixed it. Want me to open the PR?")
        self.assertEqual(item["ws_title"], "assistant (fix/comms)",
                         "a default-titled workspace falls back to the session's title")

    def test_idle_notification_after_a_status_update_is_skipped(self):
        """Claude's idle alert fires about a minute after every turn. When the
        turn ended with a status update, nobody needs to act, so no ping
        (2026-09-28: 451 such pings in two weeks). Mutation probe: drop the
        asks_user gate and this drops an item."""
        bank, state, resolver = self._components()
        reader = SessionReaderSpy(last_text="Standing by for the four gate agents. I'll resume.")
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="rN3"), bank, state, resolver,
            screen_reader=lambda ws: "waiting", session_reader=reader)
        self.assertIsNone(res)
        self.assertEqual(list(self.inbox.glob("cmux-*.json")) if self.inbox.exists() else [], [])

    def test_idle_notification_with_an_unreadable_transcript_still_pings(self):
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="rN4"), bank, state, resolver,
            screen_reader=lambda ws: "waiting", session_reader=SessionReaderSpy(unreadable=True))
        item = json.loads(Path(res["path"]).read_text())
        self.assertNotIn("ws_title", item)
        self.assertNotIn("last_message", item)

    def test_skipped_idle_notification_doesnt_use_up_the_cooldown(self):
        bank = self.mod.PatternBank(self.assistant / "pattern_bank.json")
        state = self.mod.WatcherState(cooldown_sec=600)
        resolver = FakeResolver()
        self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="a"), bank, state, resolver,
            screen_reader=lambda ws: "", session_reader=SessionReaderSpy(last_text="Done."))
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="b"), bank, state, resolver,
            screen_reader=lambda ws: "", session_reader=SessionReaderSpy(last_text="Merge it?"))
        self.assertIsNotNone(res)

    def test_turn_end_drop_carries_title_and_last_text(self):
        bank, state, resolver = self._components(title="Green E2E Suite")
        reader = SessionReaderSpy(last_text="Done. PR #321 opened; CI is running.")
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rS2", session_id="S-3"),
            bank, state, resolver,
            screen_reader=lambda ws: "some screen",
            session_reader=reader)
        self.assertEqual(reader.calls, [("/x", "S-3")])
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["ws_title"], "Green E2E Suite")
        self.assertEqual(item["last_message"], "Done. PR #321 opened; CI is running.")


    def test_turn_end_matches_the_agents_message_not_old_scrollback(self):
        """Patterns match what the agent just said, not 50 lines of screen where
        old output ("CI is red" from an earlier run) sits. Mutation probe:
        match the screen, or screen plus message, and the first call drops."""
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rS9"), bank, state, resolver,
            screen_reader=lambda ws: "earlier run: CI is red\nPR #12 opened",
            session_reader=SessionReaderSpy(last_text="Summarized the design docs."))
        self.assertIsNone(res)
        res = self.mod.handle_event(
            _evt("agent.hook.Stop", request_id="rS10"), bank, state, resolver,
            screen_reader=lambda ws: "",
            session_reader=SessionReaderSpy(last_text="Heads up: CI is red on main."))
        self.assertEqual(res["pattern_matched"], "ci-red")
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["screen_snippet"], "Heads up: CI is red on main.",
                         "an empty screen falls back to the message for the snippet")

    def test_permission_prompt_pings_even_after_a_status_line(self):
        """cmux sends a permission prompt as the same bare Notification as the
        idle alert. A tool call still waiting means a prompt is up. Mutation
        probe: drop the pending_tool check and this is skipped."""
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="rP"), bank, state, resolver,
            screen_reader=lambda ws: "",
            session_reader=SessionReaderSpy(last_text="Committing the fix now.",
                                            pending_tool="Bash"))
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["last_message"], "Waiting for your OK to use Bash.")

    def test_session_that_died_on_an_api_error_pings(self):
        bank, state, resolver = self._components()
        res = self.mod.handle_event(
            _evt("agent.hook.Notification", request_id="rE"), bank, state, resolver,
            screen_reader=lambda ws: "",
            session_reader=SessionReaderSpy(last_text="API Error: 529 overloaded", api_error=True))
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["last_message"], "API Error: 529 overloaded")

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
        resolver = FakeResolver(ref="workspace:5")
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


# ─── screen snippet filtering ─────────────────────────────────────────────────

class TestScreenSnippet(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        home = Path(self._tmp.name)
        self.mod = load_module("cmux_watcher_snip", "bin/cmux-watcher.py", {
            "HOME": str(home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(home / ".assistant"),
            "CMUX_PATTERN_BANK": str(home / ".assistant" / "pattern_bank.json"),
        })

    def tearDown(self):
        self._tmp.cleanup()

    def test_drops_claude_code_chrome_keeps_content(self):
        # Every noise line sits after real content, so a filter that stops
        # working pushes its line into the snippet.
        screen = "\n".join([
            "⏺ Merged #367 after both reviews came back clean.",
            "  │ PR   │ State  │ Tests │",
            "❯ Great, leave this as a comment on #273",
            "✽ Boogieing… (12m 1s · ↓ 48.9k tokens)",
            "✻ Baked for 2m 10s · done 10:40 PM",
            "✻ Brewed for 15m 33s · done 8:28 AM · 4 messages hidden (/focus to show)",
            "❯",
            "❯   ",
            "  architect-ffp/wt-348 fix/x │ ●114 ●1 │ context 43% │ $85.54 │ #6746dc4f",
            "wt-ptt │ context 58% │ $129.24 │ #6746dc4f",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
            "─────────────────────",
            " " * 60 + "✔ Update installed · Restart to update",
            " v1.0.85 downloaded · run /restart to apply · ? help",
            "┃",
            "╹▀▀▀▀▀▀▀▀━━━",
            "╻▄▄▄▄▄▄▄▄",
            "  ┌────┬────┐",
            "  ├────┼────┤",
            "  └────┴────┘",
        ])
        self.assertEqual(self.mod.last_lines(screen, n=10), "\n".join([
            "⏺ Merged #367 after both reviews came back clean.",
            "  │ PR   │ State  │ Tests │",
            "❯ Great, leave this as a comment on #273",
        ]))

    def test_keeps_lines_that_only_look_like_chrome(self):
        screen = "\n".join([
            "⏺ Checking CI… still running",
            "- Fixed for 3 users, reverted for 2m users",
            "· Reading 3 files…",
            "a │ b",
        ])
        self.assertEqual(self.mod.last_lines(screen, n=10), screen)


# ─── workspace title resolution ───────────────────────────────────────────────

class TestWsRefResolver(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        home = Path(self._tmp.name)
        self.mod = load_module("cmux_watcher_res", "bin/cmux-watcher.py", {
            "HOME": str(home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(home / ".assistant"),
            "CMUX_PATTERN_BANK": str(home / ".assistant" / "pattern_bank.json"),
        })

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_ref_and_human_title(self):
        # The live `cmux rpc workspace.list` shape: title carries a " [NN]" suffix.
        listing = {"window_id": "W", "workspaces": [
            {"id": "aaaa-1", "ref": "workspace:244", "title": "Green E2E Suite [244]"},
            {"id": "bbbb-2", "ref": "workspace:7", "title": ""},
            {"id": "cccc-3", "ref": "workspace:259", "title": "Terminal [259]"},
        ]}
        with mock.patch.object(self.mod, "_run", return_value=(0, json.dumps(listing), "")):
            resolver = self.mod.WsRefResolver(clock=lambda: 1000.0)
            self.assertEqual(resolver.resolve("AAAA-1"), "workspace:244")
            self.assertEqual(resolver.title("AAAA-1"), "Green E2E Suite")
            self.assertEqual(resolver.resolve("bbbb-2"), "workspace:7")
            self.assertIsNone(resolver.title("bbbb-2"), "an empty title is not stored")
            self.assertIsNone(resolver.title("cccc-3"), "cmux's default name says nothing")
            self.assertIsNone(resolver.title(None))

    def test_human_title_strips_only_the_ref_suffix(self):
        self.assertEqual(self.mod.human_title("Fix [WIP] ruler [12]"), "Fix [WIP] ruler")
        self.assertEqual(self.mod.human_title(None), "")


# ─── agent's last message (transcript tail) ───────────────────────────────────

def _assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _user(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


def _ask(tool_id, question):
    return {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion",
            "input": {"questions": [{"question": question, "header": "h",
                                     "options": [{"label": "a"}, {"label": "b"}]}]}}


def _answer(tool_id):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "a"}


class TestLastMessage(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.mod = load_module("cmux_watcher_msg", "bin/cmux-watcher.py", {
            "HOME": str(self.home),
            "CMUX_WATCHER_ASSISTANT_DIR": str(self.home / ".assistant"),
            "CMUX_PATTERN_BANK": str(self.home / ".assistant" / "pattern_bank.json"),
        })
        self.projects = self.home / ".claude" / "projects"

    def tearDown(self):
        self._tmp.cleanup()

    def _transcript(self, slug, session_id, records):
        d = self.projects / slug
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{session_id}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in records))
        return p

    def test_project_slug_maps_every_non_alphanumeric(self):
        cwd = self.home / "dev" / "assistant" / ".worktrees" / "comms_fix"
        cwd.mkdir(parents=True)
        expected = os.path.realpath(str(cwd)).replace("/", "-").replace(".", "-").replace("_", "-")
        self.assertEqual(self.mod.agent_session.claude_project_slug(str(cwd)), expected)
        self.assertIn("--worktrees-comms-fix", expected)

    def test_transcript_path_direct_hit_skips_the_scan(self):
        cwd = "/Users/me/dev/assistant/.worktrees/x"
        want = self._transcript(self.mod.agent_session.claude_project_slug(cwd), "sess-1", [])
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("scanned all dirs")):
            self.assertEqual(self.mod.transcript_path(cwd, "sess-1", self.projects), want)

    def test_transcript_path_finds_session_after_cwd_drift(self):
        want = self._transcript("-Users-me-launch-dir", "sess-2", [])
        self.assertEqual(self.mod.transcript_path("/Users/me/elsewhere", "sess-2", self.projects), want)
        self.assertEqual(self.mod.transcript_path(None, "sess-2", self.projects), want)
        self.assertIsNone(self.mod.transcript_path("/Users/me/elsewhere", "sess-404", self.projects))

    def test_transcript_path_rejects_unsafe_or_missing_session_id(self):
        self._transcript("-a", "ok", [])
        # Without the id check this path-walks back into -a/ and finds ok.jsonl.
        self.assertIsNone(self.mod.transcript_path("/a", "../-a/ok", self.projects))
        self.assertIsNone(self.mod.transcript_path("/a", None, self.projects))

    def test_tail_records_reads_only_the_tail_and_skips_junk(self):
        p = self.projects / "t.jsonl"
        p.parent.mkdir(parents=True)
        old = json.dumps({"n": "old", "pad": "x" * 500})
        p.write_text("\n".join([old, "not json", "[1, 2]", json.dumps({"n": "new"})]) + "\n")
        self.assertEqual(self.mod.tail_records(p, max_bytes=200), [{"n": "new"}])
        self.assertEqual([r["n"] for r in self.mod.tail_records(p)], ["old", "new"])

    def test_pending_question_returns_unanswered_newest(self):
        records = [
            _assistant(_ask("t1", "Old question?")),
            _user(_answer("t1")),
            _assistant({"type": "text", "text": "Two ways to go."}),
            _assistant(_ask("t2", "Should I rebase or merge main?")),
        ]
        self.assertEqual(self.mod.pending_question(records), "Should I rebase or merge main?")

    def test_pending_question_none_when_newest_is_answered(self):
        records = [_assistant(_ask("t1", "Old question?")), _user(_answer("t1"))]
        self.assertIsNone(self.mod.pending_question(records))

    def test_pending_question_none_for_missing_or_blank_text(self):
        blank = _ask("t1", "   ")
        empty = {"type": "tool_use", "id": "t2", "name": "AskUserQuestion", "input": {}}
        self.assertIsNone(self.mod.pending_question([_assistant(blank)]))
        self.assertIsNone(self.mod.pending_question([_assistant(empty)]))
        self.assertIsNone(self.mod.pending_question([_assistant({"type": "text", "text": "hi"})]))

    def test_last_assistant_text_skips_user_and_non_text_blocks(self):
        records = [
            _assistant({"type": "text", "text": "an earlier turn's words"}),
            _user({"type": "text", "text": "the user's prompt"}),
            _assistant({"type": "text", "text": "Opened the PR."}),
            _assistant({"type": "text", "text": "  "}, {"type": "tool_use", "name": "Bash",
                                                       "input": {}}),
            {"type": "assistant", "message": {"role": "assistant", "content": "plain string"}},
            {"type": "summary", "summary": "not a turn"},
            _assistant({"type": "tool_result", "text": "tool output, not the agent"}),
            _user({"type": "tool_result", "tool_use_id": "t1", "content": "ok"}),
        ]
        self.assertEqual(self.mod.last_assistant_text(records), "Opened the PR.")
        self.assertIsNone(self.mod.last_assistant_text([_user({"type": "text", "text": "x"})]))

    def test_last_assistant_text_ignores_earlier_turns(self):
        """A turn with only tool calls must not re-judge the previous turn's
        words ("PR #12 opened" would fire again)."""
        records = [_assistant({"type": "text", "text": "PR #12 opened."}),
                   _user({"type": "text", "text": "next task"}),
                   _assistant({"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}),
                   _user({"type": "tool_result", "tool_use_id": "t2", "content": "done"})]
        self.assertIsNone(self.mod.last_assistant_text(records))
        typed = {"type": "user", "message": {"role": "user", "content": "plain prompt"}}
        hook = {"type": "user", "message": {"role": "user", "content": "<local-command>"}}
        meta = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "x"}}
        self.assertEqual(self.mod.current_turn([_assistant(), typed, hook, meta]), [hook, meta])

    def test_pending_tool_names_the_call_waiting_on_a_prompt(self):
        waiting = [_user({"type": "text", "text": "go"}),
                   _assistant({"type": "text", "text": "Committing now."},
                              {"type": "tool_use", "id": "b1", "name": "Bash", "input": {}})]
        self.assertEqual(self.mod.pending_tool(waiting), "Bash")
        done = [*waiting, _user({"type": "tool_result", "tool_use_id": "b1", "content": "ok"})]
        self.assertIsNone(self.mod.pending_tool(done))
        nameless = [_assistant({"type": "tool_use", "id": "b2", "input": {}})]
        self.assertEqual(self.mod.pending_tool(nameless), "a tool")

    def test_ended_on_api_error(self):
        err = {"type": "assistant", "isApiErrorMessage": True,
               "message": {"role": "assistant", "content": [{"type": "text", "text": "API Error: 403"}]}}
        self.assertTrue(self.mod.ended_on_api_error([_user({"type": "text", "text": "go"}), err]))
        self.assertFalse(self.mod.ended_on_api_error([err, _assistant({"type": "text", "text": "ok"})]))
        self.assertFalse(self.mod.ended_on_api_error([_user({"type": "text", "text": "go"})]))
        after_result = [_user({"type": "text", "text": "go"}), err,
                        _user({"type": "tool_result", "tool_use_id": "x", "content": "ok"})]
        self.assertTrue(self.mod.ended_on_api_error(after_result))

    def test_prompt_message_quotes_the_open_prompt(self):
        pm = self.mod.prompt_message
        self.assertEqual(pm({"pending_tool": "AskUserQuestion", "question": "Merge?"}), "Merge?")
        self.assertEqual(pm({"pending_tool": "ExitPlanMode"}), "Waiting for you to approve its plan.")
        self.assertEqual(pm({"pending_tool": "Bash", "last_text": "Committing."}),
                         "Waiting for your OK to use Bash.")
        self.assertEqual(pm({"pending_tool": "Bash", "last_text": "Run it?"}), "Run it?")
        self.assertEqual(pm({"last_text": "Done."}), "Done.")

    def test_trim_words(self):
        self.assertEqual(self.mod.trim_words("a\n\n  b"), "a b")
        long = "word " * 100
        trimmed = self.mod.trim_words(long, limit=23)
        self.assertEqual(trimmed, "word word word word…")

    def test_read_session_end_to_end(self):
        cwd = "/Users/me/dev/proj"
        records = [
            {"type": "ai-title", "aiTitle": "Rebase the ruler fix", "sessionId": "sess-9"},
            _assistant({"type": "text", "text": "I found two ways.\n\nPick one."}),
            _assistant(_ask("t9", "Should I rebase or merge main?")),
        ]
        self._transcript(self.mod.agent_session.claude_project_slug(cwd), "sess-9", records)
        self.assertEqual(self.mod.read_session(cwd, "sess-9"), {
            "question": "Should I rebase or merge main?",
            "last_text": "I found two ways.\n\nPick one.",
            "pending_tool": "AskUserQuestion",
            "api_error": False,
            "title": "Rebase the ruler fix"})
        self.assertIsNone(self.mod.read_session(cwd, "sess-404"))

    def test_live_payload_shape_finds_the_question(self):
        # Live cmux payloads prefix the id (`claude-<uuid>`) while the file is
        # `<uuid>.jsonl`, and the hook cwd drifts from the launch project dir.
        uuid = "6746dc4f-9eca-4f38-9eb6-89a126ff3b53"
        self._transcript("-Users-me-dev-architect-ffp", uuid,
                         [_assistant(_ask("t1", "Should I rebase or merge main?"))])
        bank = self.mod.PatternBank(self.home / ".assistant" / "pattern_bank.json")
        res = self.mod.handle_event(
            _evt("agent.hook.AskUserQuestion", cwd="/private/tmp/wt-261",
                 session_id=f"claude-{uuid}"),
            bank, self.mod.WatcherState(cooldown_sec=0), FakeResolver(title="T"),
            screen_reader=lambda ws: "Which option?")
        item = json.loads(Path(res["path"]).read_text())
        self.assertEqual(item["last_message"], "Should I rebase or merge main?")

    def test_read_session_with_nothing_to_say(self):
        cwd = "/Users/me/dev/quiet"
        self._transcript(self.mod.agent_session.claude_project_slug(cwd), "sess-q",
                         [_user({"type": "text", "text": "hello?"})])
        self.assertEqual(self.mod.read_session(cwd, "sess-q"),
                         {"question": None, "last_text": None, "pending_tool": None,
                          "api_error": False, "title": None})

    def test_read_session_never_raises(self):
        # No ~/.claude/projects at all: the scan fails, the drop must not.
        self.assertIsNone(self.mod.read_session("/nowhere", "sess-x"))
        self.assertFalse(self.projects.exists())

    def test_session_title_prefers_claudes_title_then_folder_and_branch(self):
        title = self.mod.session_title
        self.assertEqual(title([{"type": "ai-title", "aiTitle": " Fix ruler "},
                                {"cwd": "/w/repo", "gitBranch": "main"}]), "Fix ruler")
        self.assertEqual(title([{"cwd": "/w/architect-ffp", "gitBranch": "fix/x"}]),
                         "architect-ffp (fix/x)")
        self.assertEqual(title([{"cwd": "/w/repo", "gitBranch": "HEAD"}]), "repo")
        self.assertEqual(title([{"cwd": "/", "gitBranch": ""}]), "/")
        self.assertEqual(title([{"type": "ai-title", "aiTitle": "  "}, {"cwd": ""}]), None)
        self.assertIsNone(title([]))

    def test_asks_user_reads_the_last_few_lines(self):
        asks = self.mod.asks_user
        self.assertTrue(asks("Done.\n\nWant me to open the PR?"))
        self.assertTrue(asks("Two options below.\nLet me know which one."))
        self.assertTrue(asks("Should I merge"))
        self.assertTrue(asks("Tear down the worktree (`cleanup`)?\n- The 3 deferrals remain open"),
                        "an ask above a closing note still counts")
        self.assertTrue(asks("Paste the ticket text, or say the word and I'll open a browser"))
        self.assertTrue(asks("Reply `cleanup` to tear down the batch4 worktree"))
        self.assertTrue(asks("Please run /login · API Error: 403"))
        self.assertTrue(asks("Ready for your approval."))
        self.assertFalse(asks("Is it fixed?\nYes.\nGates green.\nStanding by for CI."),
                          "a question four lines up is history, not an ask")
        self.assertFalse(asks("Standing by for the four gate agents."))
        self.assertFalse(asks(""))
        self.assertFalse(asks(None))


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
