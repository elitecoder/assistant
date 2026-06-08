"""Direct-import tests for bin/build-ws-context.py.

Existing test_build_ws_context.py runs the CLI via subprocess (good for
end-to-end CLI shape, but coverage shows 0%). This file imports the
module directly and exercises every code path, with heavy emphasis on the
ABSOLUTE INVARIANT: never attach a transcript from an old or wrong workspace.

  - status_bar_session_id: reads the id ONLY from a status-bar-shaped line,
    not from #<8hex> in conversation content; last-match-wins.
  - find_agent_pane: picks the Claude pane out of a multi-pane workspace, not
    the focused shell; falls back to a booting agent.
  - transcript_from_session_id: glob + internal-sessionId verification.
  - registry_transcript_for_surface: live-pid gate, screen-agreement gate,
    internal-sid gate — rejects the stale-registry trap.
  - resolve_workspace_screen_and_transcript: end-to-end, never-guess priority.
  - transcript_signals / cwd_state / screen_shows_error / main().
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/build-ws-context.py"


def load_module(home: Path):
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("build_ws_context_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / "Library/Application Support/cmux").mkdir(parents=True)
    (tmp / ".claude/projects").mkdir(parents=True)
    return tmp


def _status_bar(sid8: str, branch: str = "main") -> str:
    """A realistic Claude status-bar line carrying the session stamp."""
    return f"  assistant {branch} │ ●3 ●1 │ context 39% │ $42.20 │ #{sid8}"


class StatusBarSessionIdTests(unittest.TestCase):
    """The #1 wrong-capture trap: a #<8hex> in CONVERSATION CONTENT must not
    be mistaken for the agent's own session id. Only the status-bar line counts."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_reads_id_from_status_bar(self):
        self.assertEqual(
            self.mod.status_bar_session_id(_status_bar("6fb0c668")), "6fb0c668")

    def test_ignores_id_in_conversation_content(self):
        # The literal ws:5 failure: the screen discusses another session
        # (#6fb0c668) above, then the real status bar (#e90df8ba) at the bottom.
        screen = (
            "⏺ I checked workspace:12 — its session is #6fb0c668 and it is stuck.\n"
            "  Here is a quote: '… │ $9.55 │ #deadbeef' from the docs.\n"
            + _status_bar("e90df8ba"))
        self.assertEqual(self.mod.status_bar_session_id(screen), "e90df8ba")

    def test_none_when_only_content_ids_present(self):
        # No status-bar line at all → None, never a content id.
        screen = ("⏺ The session #6fb0c668 had an issue.\n"
                  "  Another ref: #abcdef12 in prose.")
        self.assertIsNone(self.mod.status_bar_session_id(screen))

    def test_last_status_bar_wins(self):
        # Scrollback may contain an older bar; the live one is at the bottom.
        screen = _status_bar("11111111") + "\n…\n" + _status_bar("22222222")
        self.assertEqual(self.mod.status_bar_session_id(screen), "22222222")

    def test_empty_and_none(self):
        self.assertIsNone(self.mod.status_bar_session_id(""))
        self.assertIsNone(self.mod.status_bar_session_id(None))


class TranscriptFromSessionIdTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _mk(self, project: str, sid_full: str, internal_sid: str | None):
        proj = self._tmp / ".claude/projects" / project
        proj.mkdir(parents=True, exist_ok=True)
        tp = proj / f"{sid_full}.jsonl"
        line = {"type": "user", "message": {"role": "user", "content": "hi"}}
        if internal_sid is not None:
            line["sessionId"] = internal_sid
        tp.write_text(json.dumps(line) + "\n")
        return tp

    def test_resolves_and_verifies_internal_sid(self):
        full = "6fb0c668-7134-4060-bcf1-6c509c9983cd"
        tp = self._mk("-Users-x-dev", full, full)
        self.assertEqual(self.mod.transcript_from_session_id("6fb0c668"), str(tp))

    def test_rejects_file_whose_internal_sid_disagrees(self):
        # Filename starts with the prefix but the file self-identifies as a
        # DIFFERENT session → must be rejected (defense against a stray/renamed
        # file). With no other candidate, returns None.
        self._mk("-Users-x-dev", "6fb0c668-aaaa-bbbb-cccc-dddddddddddd",
                 "99999999-0000-0000-0000-000000000000")
        self.assertIsNone(self.mod.transcript_from_session_id("6fb0c668"))

    def test_accepts_legacy_file_with_no_internal_sid(self):
        full = "6fb0c668-7134-4060-bcf1-6c509c9983cd"
        tp = self._mk("-Users-x-dev", full, None)  # no sessionId field
        self.assertEqual(self.mod.transcript_from_session_id("6fb0c668"), str(tp))

    def test_none_when_no_file(self):
        self.assertIsNone(self.mod.transcript_from_session_id("deadbeef"))

    def test_none_for_null_prefix(self):
        self.assertIsNone(self.mod.transcript_from_session_id(None))

    def test_newest_verified_match_wins_on_prefix_collision(self):
        full_old = "abcd1234-1111-1111-1111-111111111111"
        full_new = "abcd1234-2222-2222-2222-222222222222"
        old = self._mk("-Users-x-dev", full_old, full_old)
        new = self._mk("-Users-x-dev-other", full_new, full_new)
        os.utime(old, (time.time() - 100, time.time() - 100))
        os.utime(new, (time.time(), time.time()))
        self.assertEqual(self.mod.transcript_from_session_id("abcd1234"), str(new))


class RegistryTranscriptTests(unittest.TestCase):
    """The stale-registry trap (ws:12): the registry maps surface→session, but
    a reused surface leaves a DEAD-pid row pointing at the old session."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _write_registry(self, entries: list[dict]):
        reg = {f"tab-{i}": e for i, e in enumerate(entries)}
        (self._tmp / ".claude/cmux-registry.json").write_text(json.dumps(reg))

    def _mk_transcript(self, sid_full: str) -> str:
        proj = self._tmp / ".claude/projects/-Users-x-dev"
        proj.mkdir(parents=True, exist_ok=True)
        tp = proj / f"{sid_full}.jsonl"
        tp.write_text(json.dumps({"sessionId": sid_full,
                                  "message": {"role": "user", "content": "hi"}}) + "\n")
        return str(tp)

    def test_live_pid_entry_resolves(self):
        full = "11112222-aaaa-bbbb-cccc-dddddddddddd"
        tp = self._mk_transcript(full)
        self._write_registry([{
            "surface_id": "SURF-A", "session_id": full,
            "claude_pid": str(os.getpid()),  # our own pid → alive
            "ts": 100, "transcript_path": tp,
        }])
        out = self.mod.registry_transcript_for_surface("surf-a")  # case-insensitive
        self.assertIsNotNone(out)
        self.assertEqual(out["transcript_path"], tp)
        self.assertEqual(out["session_id8"], "11112222")

    def test_dead_pid_entry_rejected(self):
        # This is the ws:12 trap: a stale row whose process is gone.
        full = "deadbeef-aaaa-bbbb-cccc-dddddddddddd"
        tp = self._mk_transcript(full)
        self._write_registry([{
            "surface_id": "SURF-A", "session_id": full,
            "claude_pid": "999999",  # not a live pid
            "ts": 100, "transcript_path": tp,
        }])
        self.assertIsNone(self.mod.registry_transcript_for_surface("SURF-A"))

    def test_disagreement_with_screen_id_rejected(self):
        # Live pid, but the registry's session != the live status-bar id →
        # the screen wins, registry path is NOT used.
        full = "11112222-aaaa-bbbb-cccc-dddddddddddd"
        tp = self._mk_transcript(full)
        self._write_registry([{
            "surface_id": "SURF-A", "session_id": full,
            "claude_pid": str(os.getpid()), "ts": 100, "transcript_path": tp,
        }])
        self.assertIsNone(self.mod.registry_transcript_for_surface(
            "SURF-A", expect_sid8="99999999"))

    def test_agreement_with_screen_id_accepted(self):
        full = "11112222-aaaa-bbbb-cccc-dddddddddddd"
        tp = self._mk_transcript(full)
        self._write_registry([{
            "surface_id": "SURF-A", "session_id": full,
            "claude_pid": str(os.getpid()), "ts": 100, "transcript_path": tp,
        }])
        out = self.mod.registry_transcript_for_surface("SURF-A", expect_sid8="11112222")
        self.assertIsNotNone(out)

    def test_missing_transcript_file_rejected(self):
        self._write_registry([{
            "surface_id": "SURF-A", "session_id": "11112222-x",
            "claude_pid": str(os.getpid()), "ts": 100,
            "transcript_path": "/no/such/file.jsonl",
        }])
        self.assertIsNone(self.mod.registry_transcript_for_surface("SURF-A"))

    def test_no_entry_for_surface(self):
        self._write_registry([])
        self.assertIsNone(self.mod.registry_transcript_for_surface("SURF-A"))

    def test_picks_newest_entry_for_surface(self):
        live = "aaaa1111-1111-1111-1111-111111111111"
        tp = self._mk_transcript(live)
        self._write_registry([
            {"surface_id": "SURF-A", "session_id": "old00000-x",
             "claude_pid": "999999", "ts": 50, "transcript_path": "/old.jsonl"},
            {"surface_id": "SURF-A", "session_id": live,
             "claude_pid": str(os.getpid()), "ts": 200, "transcript_path": tp},
        ])
        out = self.mod.registry_transcript_for_surface("SURF-A")
        self.assertEqual(out["session_id8"], "aaaa1111")


class TranscriptSignalsTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_returns_none_idle_when_path_missing(self):
        age, status = self.mod.transcript_signals(None)
        self.assertIsNone(age)
        self.assertEqual(status, "idle")

        age, status = self.mod.transcript_signals("/no/such/file")
        self.assertIsNone(age)
        self.assertEqual(status, "idle")

    def test_idle_when_no_pending_tool_use(self):
        path = self._tmp / "t.jsonl"
        path.write_text("\n".join([
            json.dumps({"message": {"content": [
                {"type": "text", "text": "hi"}]}}),
        ]))
        age, status = self.mod.transcript_signals(str(path))
        self.assertIsInstance(age, int)
        self.assertEqual(status, "idle")

    def test_working_when_tool_use_pending(self):
        path = self._tmp / "t.jsonl"
        path.write_text(json.dumps({"message": {"content": [
            {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {}}]}}))
        _, status = self.mod.transcript_signals(str(path))
        self.assertEqual(status, "working")

    def test_idle_when_tool_use_followed_by_result(self):
        path = self._tmp / "t.jsonl"
        path.write_text("\n".join([
            json.dumps({"message": {"content": [
                {"type": "tool_use", "id": "tu-1"}]}}),
            json.dumps({"message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"}]}}),
        ]))
        _, status = self.mod.transcript_signals(str(path))
        self.assertEqual(status, "idle")

    def test_age_reflects_mtime(self):
        path = self._tmp / "t.jsonl"
        path.write_text("{}")
        os.utime(path, (time.time() - 500, time.time() - 500))
        age, _ = self.mod.transcript_signals(str(path))
        self.assertGreaterEqual(age, 499)


class CwdStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_returns_false_false_when_cwd_missing(self):
        d, u = self.mod.cwd_state(None)
        self.assertEqual((d, u), (False, False))

        d, u = self.mod.cwd_state("/no/such/dir-x")
        self.assertEqual((d, u), (False, False))

    def test_clean_repo(self):
        # Init a real git repo + empty commit so @{u} doesn't error
        # (subprocess just returns rc != 0 when no upstream — we treat
        # that as unpushed=False).
        repo = self._tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo))
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(repo))
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                       cwd=str(repo), check=True)
        d, u = self.mod.cwd_state(str(repo))
        self.assertFalse(d)
        self.assertFalse(u)

    def test_dirty_repo(self):
        repo = self._tmp / "repo2"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        (repo / "f.txt").write_text("hello")
        d, _ = self.mod.cwd_state(str(repo))
        self.assertTrue(d)


class SurfaceHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_read_surface_passes_window_context_and_scrollback(self):
        # A surface ref needs --workspace as window context, or cmux errors
        # "Surface is not a terminal".
        fake = mock.Mock(returncode=0, stdout="ok")
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake) as run:
            self.mod._read_surface("surface:15", "workspace:12")
        argv = run.call_args[0][0]
        self.assertIn("--surface", argv)
        self.assertIn("surface:15", argv)
        self.assertIn("--workspace", argv)
        self.assertIn("workspace:12", argv)
        self.assertIn("--scrollback", argv)

    def test_read_surface_nonterminal_returns_empty(self):
        fake = mock.Mock(returncode=1, stdout="Error: Surface is not a terminal")
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            self.assertEqual(self.mod._read_surface("surface:15", "workspace:12"), "")

    def test_list_panes_parses_refs(self):
        out = ("* pane:20 UUID-A  [1 surface]  [focused]\n"
               "  pane:21 UUID-B  [1 surface]\n")
        fake = mock.Mock(returncode=0, stdout=out)
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            self.assertEqual(self.mod._list_panes("workspace:9"),
                             ["pane:20", "pane:21"])

    def test_list_panes_empty_on_error(self):
        fake = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            self.assertEqual(self.mod._list_panes("workspace:9"), [])

    def test_pane_surfaces_extracts_uuid(self):
        out = "* surface:21 DE7AC3CD-C1F1-4CA8-82FD-225E12FB6749  title  [selected]\n"
        fake = mock.Mock(returncode=0, stdout=out)
        with mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            pairs = self.mod._pane_surfaces("workspace:9", "pane:20")
        self.assertEqual(pairs, [("surface:21", "DE7AC3CD-C1F1-4CA8-82FD-225E12FB6749")])


class FindAgentPaneTests(unittest.TestCase):
    """The multi-pane trap: the agent is NOT always the focused/first pane.
    find_agent_pane must enumerate all panes and pick the Claude one."""

    CLAUDE = ("❯ \n"
              "  assistant main │ ●3 ●1 │ context 39% │ $42.20 │ #6fb0c668\n"
              "  ⏵⏵ bypass permissions on (shift+tab to cycle)")
    BOOTING = ("▝▜█████▛▘  Opus 4.8 (1M context)\n"
               "  Welcome back\n  ❯ ")   # banner, no status bar yet
    SHELL = "$ tail -f server.log\n  GET /api 200 12ms"

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _patch(self, panes, surfaces_by_pane, screen_by_surface):
        # surfaces_by_pane: {pane_ref: [(surface_ref, uuid), ...]}
        # screen_by_surface: {surface_ref: text}
        return (
            mock.patch.object(self.mod, "_list_panes", return_value=panes),
            mock.patch.object(self.mod, "_pane_surfaces",
                              side_effect=lambda w, p: surfaces_by_pane.get(p, [])),
            mock.patch.object(self.mod, "_read_surface",
                              side_effect=lambda s, w, lines=120: screen_by_surface.get(s, "")),
        )

    def test_empty_ws_ref_returns_none_without_shelling_out(self):
        with mock.patch.object(self.mod.subprocess, "run") as run:
            self.assertIsNone(self.mod.find_agent_pane(""))
            run.assert_not_called()

    def test_picks_agent_pane_not_focused_shell(self):
        # pane:20 (focused) is a shell; pane:21 is the agent. Agent must win.
        p = self._patch(
            panes=["pane:20", "pane:21"],
            surfaces_by_pane={"pane:20": [("surface:1", "U1")],
                              "pane:21": [("surface:2", "U2")]},
            screen_by_surface={"surface:1": self.SHELL, "surface:2": self.CLAUDE})
        with p[0], p[1], p[2]:
            res = self.mod.find_agent_pane("workspace:15")
        self.assertEqual(res["surface_ref"], "surface:2")
        self.assertEqual(res["surface_uuid"], "U2")
        self.assertEqual(res["sid8"], "6fb0c668")
        self.assertIn("bypass permissions", res["screen_text"])

    def test_prefers_status_bar_pane_over_booting_pane(self):
        # One pane is a booting agent (banner, no sid), another has a live bar.
        # The live-bar one wins because it carries a verifiable session id.
        p = self._patch(
            panes=["pane:1", "pane:2"],
            surfaces_by_pane={"pane:1": [("surface:1", "U1")],
                              "pane:2": [("surface:2", "U2")]},
            screen_by_surface={"surface:1": self.BOOTING, "surface:2": self.CLAUDE})
        with p[0], p[1], p[2]:
            res = self.mod.find_agent_pane("workspace:9")
        self.assertEqual(res["sid8"], "6fb0c668")
        self.assertEqual(res["surface_ref"], "surface:2")

    def test_booting_agent_returned_when_no_status_bar_anywhere(self):
        # Only a booting agent + a shell → return the booting agent (sid None),
        # so we still hand the Observer the agent's screen.
        p = self._patch(
            panes=["pane:1", "pane:2"],
            surfaces_by_pane={"pane:1": [("surface:1", "U1")],
                              "pane:2": [("surface:2", "U2")]},
            screen_by_surface={"surface:1": self.SHELL, "surface:2": self.BOOTING})
        with p[0], p[1], p[2]:
            res = self.mod.find_agent_pane("workspace:9")
        self.assertEqual(res["surface_ref"], "surface:2")
        self.assertIsNone(res["sid8"])

    def test_no_agent_pane_returns_none(self):
        # All shells → None. No agent ⇒ no transcript attached (the invariant).
        p = self._patch(
            panes=["pane:1"],
            surfaces_by_pane={"pane:1": [("surface:1", "U1")]},
            screen_by_surface={"surface:1": self.SHELL})
        with p[0], p[1], p[2]:
            self.assertIsNone(self.mod.find_agent_pane("workspace:9"))

    def test_falls_back_to_focused_surfaces_when_list_panes_empty(self):
        # Old cmux: list-panes returns nothing → fall back to focused pane's
        # surfaces via the workspace-level list-pane-surfaces.
        out = "* surface:6 709C2DC8-703B-4F7C-BBE6-47F46FB69B22  title  [selected]\n"
        def fake_run(args, **kw):
            if "list-panes" in args:
                return mock.Mock(returncode=0, stdout="")
            if "list-pane-surfaces" in args:
                return mock.Mock(returncode=0, stdout=out)
            return mock.Mock(returncode=1, stdout="")
        with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(self.mod, "_read_surface", return_value=self.CLAUDE):
                res = self.mod.find_agent_pane("workspace:9")
        self.assertEqual(res["sid8"], "6fb0c668")
        self.assertEqual(res["surface_ref"], "surface:6")

    def test_oversized_screen_keeps_tail(self):
        big = "X" * 20000 + "\n" + self.CLAUDE
        p = self._patch(
            panes=["pane:1"],
            surfaces_by_pane={"pane:1": [("surface:1", "U1")]},
            screen_by_surface={"surface:1": big})
        with p[0], p[1], p[2]:
            res = self.mod.find_agent_pane("workspace:9")
        self.assertLess(len(res["screen_text"]), 13000)
        self.assertIn("earlier screen truncated", res["screen_text"])
        self.assertEqual(res["sid8"], "6fb0c668")


class ResolveWorkspaceTests(unittest.TestCase):
    """End-to-end resolution priority — and the central guarantee: a wrong
    transcript is never emitted; the worst case is transcript_path=None."""

    CLAUDE = ("  assistant main │ ●3 ●1 │ context 39% │ $42.20 │ #6fb0c668\n"
              "  ⏵⏵ bypass permissions on")

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _mk_transcript(self, sid_full, project="-Users-x-dev"):
        proj = self._tmp / ".claude/projects" / project
        proj.mkdir(parents=True, exist_ok=True)
        tp = proj / f"{sid_full}.jsonl"
        tp.write_text(json.dumps({"sessionId": sid_full,
                                  "message": {"role": "user", "content": "hi"}}) + "\n")
        return str(tp)

    def test_screen_session_id_is_primary(self):
        full = "6fb0c668-7134-4060-bcf1-6c509c9983cd"
        tp = self._mk_transcript(full)
        with mock.patch.object(self.mod, "find_agent_pane", return_value={
                "surface_ref": "surface:2", "surface_uuid": "U2",
                "screen_text": self.CLAUDE, "sid8": "6fb0c668"}):
            res = self.mod.resolve_workspace_screen_and_transcript("workspace:12")
        self.assertEqual(res["transcript_source"], "screen_session_id")
        self.assertEqual(res["transcript_path"], tp)
        self.assertEqual(res["session_id8"], "6fb0c668")

    def test_falls_back_to_registry_when_no_screen_id_but_pid_alive(self):
        # Booting agent: status bar not rendered yet (sid None), but the
        # registry has a LIVE-pid row for its surface UUID.
        full = "1d27a528-aaaa-bbbb-cccc-dddddddddddd"
        tp = self._mk_transcript(full)
        reg = {"tab-0": {"surface_id": "U2", "session_id": full,
                         "claude_pid": str(os.getpid()), "ts": 100,
                         "transcript_path": tp}}
        (self._tmp / ".claude/cmux-registry.json").write_text(json.dumps(reg))
        with mock.patch.object(self.mod, "find_agent_pane", return_value={
                "surface_ref": "surface:2", "surface_uuid": "U2",
                "screen_text": "booting…", "sid8": None}):
            res = self.mod.resolve_workspace_screen_and_transcript("workspace:9")
        self.assertEqual(res["transcript_source"], "registry_live_pid")
        self.assertEqual(res["transcript_path"], tp)
        self.assertEqual(res["session_id8"], "1d27a528")

    def test_stale_registry_never_wins_over_live_screen(self):
        # THE ws:12 SCENARIO: live screen says 6fb0c668, but the registry's
        # surface row is a DEAD-pid pointer at a different (old) session. The
        # screen id resolves; the stale registry path is never emitted.
        live = "6fb0c668-7134-4060-bcf1-6c509c9983cd"
        stale = "b5f8d913-a9cb-433e-989d-63df9e14c253"
        live_tp = self._mk_transcript(live)
        stale_tp = self._mk_transcript(stale, project="-Users-x-dev-assistant")
        reg = {"tab-0": {"surface_id": "U2", "session_id": stale,
                         "claude_pid": "999999",  # dead
                         "ts": 100, "transcript_path": stale_tp}}
        (self._tmp / ".claude/cmux-registry.json").write_text(json.dumps(reg))
        with mock.patch.object(self.mod, "find_agent_pane", return_value={
                "surface_ref": "surface:2", "surface_uuid": "U2",
                "screen_text": self.CLAUDE, "sid8": "6fb0c668"}):
            res = self.mod.resolve_workspace_screen_and_transcript("workspace:12")
        self.assertEqual(res["transcript_path"], live_tp)
        self.assertNotIn("assistant", res["transcript_path"])  # not the stale dir
        self.assertEqual(res["transcript_source"], "screen_session_id")

    def test_no_verified_signal_yields_null_transcript(self):
        # Agent found, but no transcript on disk yet and no registry row →
        # transcript_path MUST be None (never a guess). screen still provided.
        with mock.patch.object(self.mod, "find_agent_pane", return_value={
                "surface_ref": "surface:2", "surface_uuid": "U2",
                "screen_text": self.CLAUDE, "sid8": "6fb0c668"}):
            res = self.mod.resolve_workspace_screen_and_transcript("workspace:9")
        self.assertIsNone(res["transcript_path"])
        self.assertIsNone(res["transcript_source"])
        self.assertEqual(res["session_id8"], "6fb0c668")  # surfaced for later
        self.assertIn("bypass permissions", res["screen_text"])

    def test_no_agent_pane_yields_everything_null(self):
        with mock.patch.object(self.mod, "find_agent_pane", return_value=None):
            res = self.mod.resolve_workspace_screen_and_transcript("workspace:9")
        self.assertIsNone(res["transcript_path"])
        self.assertIsNone(res["session_id8"])
        self.assertEqual(res["screen_text"], "")
        self.assertFalse(res["screen_shows_error"])


class ScreenShowsErrorTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_detects_api_error_banner(self):
        # The literal ws:12 banner: an assistant turn (⏺) that ended on the error.
        self.assertTrue(self.mod.screen_shows_error(
            "⏺ API Error: The system encountered an unexpected error during processing."))

    def test_detects_banner_amid_other_lines(self):
        screen = ("  some earlier output\n"
                  "⏺ Request timed out after 240s\n"
                  "❯ ")
        self.assertTrue(self.mod.screen_shows_error(screen))

    def test_detects_overloaded_and_rate_limit_banners(self):
        self.assertTrue(self.mod.screen_shows_error("⏺ overloaded_error: try again"))
        self.assertTrue(self.mod.screen_shows_error("· Rate limit reached"))

    def test_prose_mentioning_errors_is_not_flagged(self):
        # The false-positive this anchoring fixes: an agent DISCUSSING errors
        # (e.g. this very session, or a recap) must not look stranded.
        self.assertFalse(self.mod.screen_shows_error(
            "I think the API Error we saw earlier was transient — retried fine."))
        self.assertFalse(self.mod.screen_shows_error(
            "  - screen_shows_error: true when the screen shows an API error banner"))

    def test_edited_code_with_error_strings_is_not_flagged(self):
        # A diff/editor view containing the literal strings must not trip it.
        self.assertFalse(self.mod.screen_shows_error(
            '    raise RuntimeError("API Error")\n    # Traceback (most recent call last)'))

    def test_clean_recap_is_not_error(self):
        self.assertFalse(self.mod.screen_shows_error(
            "All 5 tasks done. Ready for your review — want me to land the PR?"))

    def test_empty_screen_is_not_error(self):
        self.assertFalse(self.mod.screen_shows_error(""))
        self.assertFalse(self.mod.screen_shows_error(None))


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    @staticmethod
    def _resolved(screen_text="", screen_shows_error=False, session_id8=None,
                  transcript_path=None, transcript_source=None, agent_surface=None):
        return {"screen_text": screen_text, "screen_shows_error": screen_shows_error,
                "session_id8": session_id8, "transcript_path": transcript_path,
                "transcript_source": transcript_source, "agent_surface": agent_surface}

    def _run_main(self, ws_ref, title, cwd, resolved=None):
        sys.argv = ["build-ws-context.py", "--ws-ref", ws_ref,
                    "--title", title, "--cwd", cwd]
        captured = io.StringIO()
        # Keep main() hermetic — never touch a live cmux in tests.
        with mock.patch.object(self.mod, "resolve_workspace_screen_and_transcript",
                               return_value=resolved or self._resolved()), \
                mock.patch("sys.stdout", captured):
            rc = self.mod.main()
        return rc, json.loads(captured.getvalue())

    def test_main_emits_full_payload(self):
        rc, d = self._run_main("workspace:3", "title", "/no/such-dir")  # protected
        self.assertEqual(rc, 0)
        self.assertEqual(d["ws_ref"], "workspace:3")
        self.assertTrue(d["is_protected"])
        self.assertIsNone(d["transcript_path"])
        self.assertIsNone(d["transcript_source"])
        self.assertIsNone(d["session_id8"])
        self.assertIsNone(d["agent_surface"])
        # cwd doesn't exist → dirty/unpushed both false.
        self.assertFalse(d["cwd_dirty"])
        self.assertFalse(d["cwd_unpushed"])
        self.assertEqual(d["screen_text"], "")
        self.assertFalse(d["screen_shows_error"])

    def test_main_surfaces_screen_error_flag(self):
        rc, d = self._run_main(
            "workspace:12", "telegram-comms (resumed) [12]", "",
            resolved=self._resolved(screen_text="⏺ API Error: unexpected error",
                                    screen_shows_error=True))
        self.assertEqual(rc, 0)
        self.assertTrue(d["screen_shows_error"])
        self.assertIn("API Error", d["screen_text"])

    def test_main_passes_through_verified_transcript(self):
        # main() emits exactly what the resolver verified — and computes
        # signals off that transcript.
        tp = str(self._tmp / "6fb0c668-x.jsonl")
        Path(tp).write_text(json.dumps({"message": {"content": [
            {"type": "text", "text": "hi"}]}}) + "\n")
        rc, d = self._run_main(
            "workspace:12", "x", "/Users/x/dev",
            resolved=self._resolved(
                screen_text="… │ #6fb0c668", session_id8="6fb0c668",
                transcript_path=tp, transcript_source="screen_session_id",
                agent_surface="surface:2"))
        self.assertEqual(d["transcript_path"], tp)
        self.assertEqual(d["transcript_source"], "screen_session_id")
        self.assertEqual(d["session_id8"], "6fb0c668")
        self.assertEqual(d["agent_surface"], "surface:2")
        self.assertEqual(d["agent_status"], "idle")  # computed from the file

    def test_main_null_transcript_when_unverified(self):
        # The invariant at the CLI boundary: an agent screen with a session id
        # but NO verified transcript → transcript_path stays null.
        rc, d = self._run_main(
            "workspace:9", "x", "",
            resolved=self._resolved(screen_text="… │ #6fb0c668",
                                    session_id8="6fb0c668"))
        self.assertIsNone(d["transcript_path"])
        self.assertIsNone(d["transcript_source"])
        self.assertEqual(d["session_id8"], "6fb0c668")
        self.assertIsNone(d["last_turn_age_sec"])
        self.assertEqual(d["agent_status"], "idle")

    def test_unprotected_ref_marked_correctly(self):
        rc, d = self._run_main("workspace:42", "title", "")
        self.assertEqual(rc, 0)
        self.assertFalse(d["is_protected"])

    def test_main_age_from_observer_summary_when_no_transcript(self):
        # When transcript=None, age falls back to observer-summary stale time.
        summary_dir = self._tmp / ".assistant/observer-summaries"
        summary_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time()) - 300
        (summary_dir / "workspace_workspace_99.json").write_text(
            json.dumps({"state_unchanged_since_ts": ts}))
        with mock.patch.dict(os.environ, {"ASSISTANT_DIR": str(self._tmp / ".assistant")}):
            rc, d = self._run_main("workspace:99", "t", "",
                                   resolved=self._resolved(session_id8="abc12345"))
        self.assertEqual(rc, 0)
        # age should be ~300 (from summary)
        self.assertIsNotNone(d["last_turn_age_sec"])
        self.assertGreater(d["last_turn_age_sec"], 200)


class TranscriptSignalsEdgeCaseTests(unittest.TestCase):
    """Cover transcript_signals edge cases: OSError on getmtime, bad JSON,
    non-dict message, content not list — and cwd_state exception paths."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_getmtime_oserror_returns_idle(self):
        with mock.patch("os.path.getmtime", side_effect=OSError("no stat")):
            with mock.patch("os.path.exists", return_value=True):
                age, status = self.mod.transcript_signals("/fake/path.jsonl")
        self.assertIsNone(age)
        self.assertEqual(status, "idle")

    def test_open_oserror_returns_age_idle(self):
        # When open() fails after getmtime succeeds, age is the mtime-based age,
        # and status is "idle" (can't read the file to detect pending tool_use).
        path = str(self._tmp / "t.jsonl")
        Path(path).write_text("")
        with mock.patch("builtins.open", side_effect=OSError("io error")):
            age, status = self.mod.transcript_signals(path)
        self.assertIsNotNone(age)  # mtime was read before the open failed
        self.assertEqual(status, "idle")

    def test_malformed_json_lines_skipped(self):
        path = str(self._tmp / "t.jsonl")
        Path(path).write_text("not json\n")
        age, status = self.mod.transcript_signals(path)
        self.assertIsNotNone(age)
        self.assertEqual(status, "idle")

    def test_message_not_dict_skipped(self):
        path = str(self._tmp / "t.jsonl")
        Path(path).write_text(json.dumps({"message": "string not dict"}) + "\n")
        age, status = self.mod.transcript_signals(path)
        self.assertEqual(status, "idle")

    def test_content_not_list_skipped(self):
        path = str(self._tmp / "t.jsonl")
        Path(path).write_text(json.dumps({"message": {"content": "not a list"}}) + "\n")
        age, status = self.mod.transcript_signals(path)
        self.assertEqual(status, "idle")

    def test_content_item_not_dict_skipped(self):
        path = str(self._tmp / "t.jsonl")
        Path(path).write_text(json.dumps({"message": {"content": ["string"]}}) + "\n")
        age, status = self.mod.transcript_signals(path)
        self.assertEqual(status, "idle")

    def test_cwd_state_git_exception_returns_false_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(self.mod.subprocess, "run",
                                   side_effect=OSError("no git")):
                dirty, unpushed = self.mod.cwd_state(d)
        self.assertFalse(dirty)
        self.assertFalse(unpushed)


class CmuxHelperEdgeCases(unittest.TestCase):
    """Test the _cmux / _is_claude_pane / _pane_surfaces / _read_surface edge
    cases that the higher-level tests don't exercise."""

    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_cmux_returns_none_on_exception(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               side_effect=Exception("cmux missing")):
            result = self.mod._cmux(["list-panes"])
        self.assertIsNone(result)

    def test_is_claude_pane_empty_string_returns_false(self):
        self.assertFalse(self.mod._is_claude_pane(""))

    def test_is_claude_pane_with_status_bar_returns_true(self):
        text = "  assistant main │ ●1 │ context 50% │ $1.00 │ #aabbccdd"
        self.assertTrue(self.mod._is_claude_pane(text))

    def test_pane_surfaces_returns_empty_on_cmux_failure(self):
        with mock.patch.object(self.mod.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            result = self.mod._pane_surfaces("workspace:1", "pane:1")
        self.assertEqual(result, [])

    def test_read_surface_returns_empty_for_empty_ref(self):
        result = self.mod._read_surface("", "workspace:1")
        self.assertEqual(result, "")

    def test_find_agent_pane_skips_empty_surface_text(self):
        # When _read_surface returns "" for a surface, it is skipped.
        # The agent pane with non-empty text still wins.
        CLAUDE = "  assistant main │ ●1 │ context 50% │ $1.00 │ #aabbccdd"
        surface_texts = {"surface:1": "", "surface:2": CLAUDE}
        with mock.patch.object(self.mod, "_list_panes", return_value=["pane:1", "pane:2"]):
            with mock.patch.object(self.mod, "_pane_surfaces",
                                   side_effect=lambda w, p: [
                                       ("surface:1", "U1")] if p == "pane:1" else [("surface:2", "U2")]):
                with mock.patch.object(self.mod, "_read_surface",
                                       side_effect=lambda s, w, lines=120: surface_texts.get(s, "")):
                    res = self.mod.find_agent_pane("workspace:9")
        self.assertIsNotNone(res)
        self.assertEqual(res["surface_ref"], "surface:2")

    def test_transcript_internal_sid_exception_returns_none(self):
        with mock.patch("builtins.open", side_effect=OSError("no file")):
            result = self.mod._transcript_internal_sid("/fake/path.jsonl")
        self.assertIsNone(result)

    def test_transcript_from_session_id_no_projects_dir(self):
        # When ~/.claude/projects doesn't exist, returns None.
        import shutil
        shutil.rmtree(self._tmp / ".claude/projects")
        result = self.mod.transcript_from_session_id("aabbccdd")
        self.assertIsNone(result)

    def test_registry_transcript_empty_uuid_returns_none(self):
        result = self.mod.registry_transcript_for_surface("")
        self.assertIsNone(result)

    def test_registry_transcript_internal_sid_disagreement(self):
        # Registry entry has a live pid, but transcript's own sid disagrees.
        tp = str(self._tmp / ".claude/projects/-p/aabbccdd-t.jsonl")
        Path(tp).parent.mkdir(parents=True, exist_ok=True)
        Path(tp).write_text(json.dumps({"sessionId": "11223344-diff"}) + "\n")
        reg = {"S1": {"surface_id": "AAAA-BBBB", "claude_pid": os.getpid(),
                      "ts": 1, "session_id": "aabbccdd", "transcript_path": tp}}
        (self._tmp / ".claude/cmux-registry.json").write_text(json.dumps(reg))
        result = self.mod.registry_transcript_for_surface("AAAA-BBBB")
        # Internal sid "11223344" doesn't start with "aabbccdd" → rejected.
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
