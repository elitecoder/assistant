"""Direct-import tests for bin/build-ws-context.py.

Existing test_build_ws_context.py runs the CLI via subprocess (good for
end-to-end CLI shape, but coverage shows 0%). This file imports the
module directly and exercises every code path:

  - find_transcript: cmux-registry primary lookup, sigil-fallback scan,
    both-fail null return
  - transcript_signals: agent_status=working when tool_use pending,
    agent_status=idle, missing-path return
  - cwd_state: dirty/clean, unpushed/clean, non-existent cwd
  - main(): full --ws-ref/--title/--cwd CLI shape
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


class FindTranscriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def _write_cmux_state(self, ws_title: str, panel_id: str):
        state = {
            "windows": [{
                "tabManager": {
                    "workspaces": [{
                        "customTitle": ws_title,
                        "panels": [{"id": panel_id}],
                    }],
                },
            }],
        }
        (self._tmp / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
        ).write_text(json.dumps(state))

    def _write_registry(self, entries: dict):
        (self._tmp / ".claude/cmux-registry.json").write_text(json.dumps(entries))

    def test_primary_lookup_via_panel_id(self):
        # Create a real transcript file the registry can point at.
        proj = self._tmp / ".claude/projects/-tmp"
        proj.mkdir()
        tp = proj / "abc.jsonl"
        tp.write_text('{"timestamp":"2026-05-28T00:00:00Z"}\n')
        self._write_cmux_state("Auto: td-001 my-task", "PANEL-A")
        self._write_registry({"PANEL-A": {"transcript_path": str(tp)}})

        result = self.mod.find_transcript("workspace:1", "Auto: td-001 my-task", "/tmp")
        self.assertEqual(result, str(tp))

    def test_primary_lookup_picks_most_recent_when_multiple_panels(self):
        proj = self._tmp / ".claude/projects/-tmp"
        proj.mkdir()
        old = proj / "old.jsonl"
        new = proj / "new.jsonl"
        old.write_text("{}")
        new.write_text("{}")
        # Touch timestamps so new is mtime-newer.
        os.utime(old, (time.time() - 60, time.time() - 60))
        os.utime(new, (time.time(), time.time()))

        self._write_cmux_state("ws-title", "PANEL-A")
        self._write_registry({
            "panel-1": {"panel_id": "PANEL-A", "transcript_path": str(old)},
            "panel-2": {"panel_id": "PANEL-A", "transcript_path": str(new)},
        })
        result = self.mod.find_transcript("workspace:1", "ws-title", "/tmp")
        self.assertEqual(result, str(new))

    def test_falls_back_to_slug_scan_when_registry_misses(self):
        # Registry empty but project dir has a JSONL with the title sigil
        # in its first user turn.
        proj = self._tmp / ".claude/projects/-Users-x-dev-firefly-platform"
        proj.mkdir(parents=True)
        tp = proj / "session.jsonl"
        tp.write_text('\n'.join([
            json.dumps({"message": {"role": "user", "content": [
                {"type": "text", "text": "Run td-007 polish task"},
            ]}}),
            "",
        ]))
        result = self.mod.find_transcript(
            "workspace:5", "Auto: td-007 polish",
            cwd="/Users/x/dev/firefly-platform",
        )
        self.assertEqual(result, str(tp))

    def test_returns_none_when_no_cwd(self):
        # Both lookups fail; no cwd → None.
        self.assertIsNone(self.mod.find_transcript("workspace:1", "title", None))

    def test_returns_none_when_project_dir_missing(self):
        self.assertIsNone(self.mod.find_transcript(
            "workspace:1", "Auto: td-007", "/no/such/dir-doesnotexist"
        ))

    def test_returns_none_when_title_has_no_sigil(self):
        # Project dir exists but title has no Pn-N / Wn / td-N / sq-wsN / AC-N sigil.
        proj = self._tmp / ".claude/projects/-Users-x-dev"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_text("{}")
        self.assertIsNone(self.mod.find_transcript(
            "workspace:1", "T3-tier3-polish", "/Users/x/dev",
        ))

    def test_returns_none_when_cmux_state_unreadable(self):
        # Plant a corrupt cmux state file; lookup should fall through to
        # the project-dir scan but find no jsonl, return None.
        (self._tmp / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
        ).write_text("{ not json")
        self.assertIsNone(self.mod.find_transcript("workspace:1", "title", "/tmp"))

    def test_skips_jsonl_files_that_fail_to_open(self):
        # Sigil scan should silently skip files it can't read and still
        # return None when nothing matches.
        proj = self._tmp / ".claude/projects/-x"
        proj.mkdir(parents=True)
        # An "unreadable" file: empty (read_text works, but no signature).
        (proj / "empty.jsonl").write_text("")
        # Plus one line with a JSONDecodeError that loop should skip.
        (proj / "broken.jsonl").write_text("not json\n")
        # Plus a non-dict message.
        (proj / "weird.jsonl").write_text(
            json.dumps({"message": "string-not-dict"}) + "\n"
        )
        # Nothing matches sigil td-007.
        self.assertIsNone(self.mod.find_transcript(
            "workspace:1", "td-007", "/x",
        ))

    def test_sigil_match_in_string_content(self):
        # Cover the `elif isinstance(content, str)` branch of the parser.
        proj = self._tmp / ".claude/projects/-x"
        proj.mkdir(parents=True)
        tp = proj / "s.jsonl"
        tp.write_text(
            json.dumps({"message": {"role": "user", "content": "td-007 marker"}}) + "\n"
        )
        result = self.mod.find_transcript("workspace:1", "td-007", "/x")
        self.assertEqual(result, str(tp))

    def test_user_seen_threshold_terminates_scan(self):
        # 5 user turns without sigil → scan stops.
        proj = self._tmp / ".claude/projects/-x"
        proj.mkdir(parents=True)
        tp = proj / "s.jsonl"
        lines = []
        for i in range(6):  # 6 user turns, but no sigil
            lines.append(json.dumps({
                "message": {"role": "user", "content": f"non-matching-{i}"},
            }))
        # Even if a later line has the sigil, the loop bailed at n=5.
        lines.append(json.dumps({
            "message": {"role": "user", "content": "td-007 here"},
        }))
        tp.write_text("\n".join(lines))
        # We only get a hit when sigil is in the FIRST 5 user turns.
        # With sigil at line 7 (after 6 non-matching user turns), no match.
        result = self.mod.find_transcript("workspace:1", "td-007", "/x")
        self.assertIsNone(result)


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


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self._tmp = fixture_home(Path(self._tmp_obj.name))
        self.mod = load_module(self._tmp)

    def tearDown(self):
        self._tmp_obj.cleanup()

    def test_main_emits_full_payload(self):
        sys.argv = ["build-ws-context.py",
                    "--ws-ref", "workspace:3",  # in PROTECTED_REFS
                    "--title", "title",
                    "--cwd", "/no/such-dir"]
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        d = json.loads(captured.getvalue())
        self.assertEqual(d["ws_ref"], "workspace:3")
        self.assertTrue(d["is_protected"])
        self.assertIsNone(d["transcript_path"])
        # cwd doesn't exist → dirty/unpushed both false.
        self.assertFalse(d["cwd_dirty"])
        self.assertFalse(d["cwd_unpushed"])

    def test_unprotected_ref_marked_correctly(self):
        sys.argv = ["build-ws-context.py",
                    "--ws-ref", "workspace:42",
                    "--title", "title",
                    "--cwd", ""]
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        d = json.loads(captured.getvalue())
        self.assertFalse(d["is_protected"])


if __name__ == "__main__":
    unittest.main()
