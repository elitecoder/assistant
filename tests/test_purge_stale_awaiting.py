"""Unit tests for bin/purge-stale-awaiting.py — exercises every drop predicate.

Runs without spawning Claude. Mocks `cmux tree --json` via PATH-injection
and supplies fixture state.json / assistant-todo.json files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin/purge-stale-awaiting.py"


def make_fake_cmux(tmpdir: Path, open_workspaces: list[str]) -> Path:
    """Write a fake `cmux` shim that returns the given list of open workspaces.
    Returns the bin dir to PATH-prepend."""
    fake_bin = tmpdir / "fake-bin"
    fake_bin.mkdir()
    cmux = fake_bin / "cmux"
    workspaces_json = json.dumps([
        {"ref": ws} for ws in open_workspaces
    ])
    cmux.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        if argv[:2] == ["tree", "--json"]:
            print(json.dumps({{
                "windows": [{{"workspaces": {workspaces_json}}}]
            }}))
            sys.exit(0)
        if argv[:1] == ["tree"]:
            for ws in {open_workspaces}:
                print(f"workspace {{ws}}")
            sys.exit(0)
        sys.exit(0)
    """))
    cmux.chmod(0o755)
    return fake_bin


def run_purge(state_path: Path, todo_path: Path, fake_bin: Path) -> dict:
    """Invoke the purger with HOME pointing to tmpdir-style structure.
    Returns the resulting state.json contents."""
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    # The purger uses CMUX_BIN env var with a hard-coded absolute default —
    # PATH alone won't redirect. Point it at our fake binary explicitly.
    env["CMUX_BIN"] = str(fake_bin / "cmux")
    home = state_path.parent.parent.parent  # state is at <home>/.claude/cache/state.json
    env["HOME"] = str(home)
    r = subprocess.run(
        ["python3", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise AssertionError(f"purger failed: rc={r.returncode}\nstderr: {r.stderr}")
    return json.loads(state_path.read_text())


def setup_fixture(tmpdir: Path, awaiting: list[dict], todos: list[dict]) -> tuple[Path, Path]:
    home = tmpdir / "home"
    cache = home / ".claude/cache"
    cache.mkdir(parents=True)
    todo_dir = home / ".claude"
    state = {
        "_meta": {"pulse_idx": 1, "ts": "2026-05-26T15:00:00Z"},
        "actions_taken": [],
        "awaiting_input": awaiting,
    }
    state_path = cache / "assistant-state.json"
    state_path.write_text(json.dumps(state, indent=2))
    todo_path = todo_dir / "assistant-todo.json"
    todo_path.write_text(json.dumps({"items": todos}, indent=2))
    return state_path, todo_path


class TestPurgeStaleAwaiting(unittest.TestCase):

    def test_drops_card_for_closed_workspace(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            awaiting = [
                {"key": "assistant:needs-you:workspace:91:foo", "tier": "T2",
                 "title": "ws:91 needs help", "detail": "..."},
                {"key": "assistant:needs-you:workspace:51:bar", "tier": "T2",
                 "title": "ws:51 keep me", "detail": "..."},
            ]
            state_path, _ = setup_fixture(tmp, awaiting, todos=[])
            fake_bin = make_fake_cmux(tmp, open_workspaces=["workspace:51"])
            result = run_purge(state_path, _, fake_bin)
            keys = {c["key"] for c in result.get("awaiting_input", [])}
            self.assertNotIn("assistant:needs-you:workspace:91:foo", keys)
            self.assertIn("assistant:needs-you:workspace:51:bar", keys)

    def test_keeps_card_when_cmux_unreachable(self):
        """If cmux returns no workspaces, we cannot tell what's closed —
        do NOT drop anything."""
        with TemporaryDirectory() as t:
            tmp = Path(t)
            awaiting = [
                {"key": "assistant:needs-you:workspace:91:foo", "tier": "T2",
                 "title": "X", "detail": "Y"},
            ]
            state_path, _ = setup_fixture(tmp, awaiting, todos=[])
            fake_bin = make_fake_cmux(tmp, open_workspaces=[])
            result = run_purge(state_path, _, fake_bin)
            keys = {c["key"] for c in result.get("awaiting_input", [])}
            self.assertIn("assistant:needs-you:workspace:91:foo", keys,
                          "must NOT purge when cmux returns empty (could be cmux down)")

    def test_drops_card_for_done_todo(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            awaiting = [
                {"key": "assistant:needs-you:workspace:51:td-100",
                 "title": "td-100 needs review", "detail": "see workspace:51"},
            ]
            todos = [
                {"id": "td-100", "status": "done", "autoDispatch": True,
                 "title": "...", "priority": "P2"},
            ]
            state_path, todo_path = setup_fixture(tmp, awaiting, todos)
            fake_bin = make_fake_cmux(tmp, open_workspaces=["workspace:51"])
            result = run_purge(state_path, todo_path, fake_bin)
            keys = {c["key"] for c in result.get("awaiting_input", [])}
            self.assertNotIn("assistant:needs-you:workspace:51:td-100", keys,
                             "card referencing done td-100 should be purged")

    def test_keeps_card_for_open_todo(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            awaiting = [
                {"key": "assistant:needs-you:workspace:51:td-200",
                 "title": "td-200 active", "detail": "..."},
            ]
            todos = [
                {"id": "td-200", "status": "open", "autoDispatch": True,
                 "title": "...", "priority": "P1"},
            ]
            state_path, todo_path = setup_fixture(tmp, awaiting, todos)
            fake_bin = make_fake_cmux(tmp, open_workspaces=["workspace:51"])
            result = run_purge(state_path, todo_path, fake_bin)
            keys = {c["key"] for c in result.get("awaiting_input", [])}
            self.assertIn("assistant:needs-you:workspace:51:td-200", keys)

    def test_empty_state_is_noop(self):
        with TemporaryDirectory() as t:
            tmp = Path(t)
            state_path, _ = setup_fixture(tmp, awaiting=[], todos=[])
            fake_bin = make_fake_cmux(tmp, open_workspaces=["workspace:51"])
            result = run_purge(state_path, _, fake_bin)
            self.assertEqual(result.get("awaiting_input", []), [])


if __name__ == "__main__":
    unittest.main()
