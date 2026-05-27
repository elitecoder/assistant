"""Unit tests for the back-off mechanism.

The back-off list (~/.assistant/back-off.json) names workspaces the
Assistant must skip entirely. Two scripts cooperate:

  bin/back-off.py        — CLI: add / remove / list
  bin/pick-ws-batch.py   — reads back-off.json, removes those refs from
                           the batch, surfaces them in `backed_off[]`

Both run without spawning Claude. We sandbox HOME to a tempdir so the
tests don't touch real user state, and PATH-inject a fake `cmux` shim.
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
PICK = REPO / "bin/pick-ws-batch.py"
BACK_OFF = REPO / "bin/back-off.py"


def make_fake_cmux(tmpdir: Path, refs: list[str]) -> Path:
    fake_bin = tmpdir / "fake-bin"
    fake_bin.mkdir()
    cmux = fake_bin / "cmux"
    payload = json.dumps([{"ref": r, "title": f"title for {r}", "current_directory": "/tmp"} for r in refs])
    cmux.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        argv = sys.argv[1:]
        if argv[:2] == ["list-workspaces", "--json"]:
            print({payload!r})
            sys.exit(0)
        sys.exit(1)
    """))
    cmux.chmod(0o755)
    return fake_bin


def run_pick(tmpdir: Path, fake_bin: Path) -> dict:
    """Run pick-ws-batch.py with HOME and CMUX overridden to point at tmpdir."""
    env = os.environ.copy()
    env["HOME"] = str(tmpdir)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    # Patch the hardcoded CMUX path to our shim by writing a wrapper module.
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(f"""\
            import sys, os, runpy
            sys.path.insert(0, {str(REPO / 'bin')!r})
            os.environ['HOME'] = {str(tmpdir)!r}
            # Monkeypatch the module's CMUX constant before exec.
            import importlib.util
            spec = importlib.util.spec_from_file_location('pick', {str(PICK)!r})
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.CMUX = {str(fake_bin / 'cmux')!r}
            mod.SUMM_DIR = os.path.join({str(tmpdir)!r}, '.assistant/observer-summaries')
            mod.BACK_OFF_PATH = os.path.join({str(tmpdir)!r}, '.assistant/back-off.json')
            mod.main()
        """)],
        env=env, capture_output=True, text=True, cwd=str(tmpdir),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pick-ws-batch failed: {proc.stderr}")
    return json.loads(proc.stdout)


def run_cli(tmpdir: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(tmpdir)
    return subprocess.run(
        [sys.executable, str(BACK_OFF), *args],
        env=env, capture_output=True, text=True, cwd=str(tmpdir),
    )


class BackOffFilterTests(unittest.TestCase):
    def test_no_back_off_file_means_no_skips(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            fake = make_fake_cmux(tmp, ["workspace:1", "workspace:2"])
            out = run_pick(tmp, fake)
            self.assertEqual(out["backed_off"], [])
            self.assertEqual(out["total_ws"], 2)
            refs = [w["ref"] for w in out["to_reclassify"]] + out["reuse_cached"]
            self.assertIn("workspace:1", refs)
            self.assertIn("workspace:2", refs)

    def test_listed_workspace_is_excluded(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            (tmp / ".assistant/back-off.json").write_text(json.dumps({
                "workspaces": [{"ws_ref": "workspace:112", "reason": "loop", "added_ts": 1779840000}]
            }))
            fake = make_fake_cmux(tmp, ["workspace:1", "workspace:112", "workspace:2"])
            out = run_pick(tmp, fake)
            self.assertEqual([w["ref"] for w in out["backed_off"]], ["workspace:112"])
            self.assertEqual(out["backed_off"][0]["reason"], "loop")
            refs = [w["ref"] for w in out["to_reclassify"]] + out["reuse_cached"]
            self.assertNotIn("workspace:112", refs, "back-off ws must not appear in batch")
            self.assertIn("workspace:1", refs)
            self.assertIn("workspace:2", refs)

    def test_malformed_back_off_file_is_ignored(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            (tmp / ".assistant/back-off.json").write_text("{ this is not json")
            fake = make_fake_cmux(tmp, ["workspace:1"])
            out = run_pick(tmp, fake)
            self.assertEqual(out["backed_off"], [])
            self.assertEqual(out["total_ws"], 1)


class BackOffCLITests(unittest.TestCase):
    def test_add_creates_file_and_appears_in_list(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            r = run_cli(tmp, "add", "workspace:7", "stuck on cleanup")
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads((tmp / ".assistant/back-off.json").read_text())
            self.assertEqual(len(data["workspaces"]), 1)
            self.assertEqual(data["workspaces"][0]["ws_ref"], "workspace:7")
            self.assertEqual(data["workspaces"][0]["reason"], "stuck on cleanup")
            r = run_cli(tmp, "list")
            self.assertIn("workspace:7", r.stdout)
            self.assertIn("stuck on cleanup", r.stdout)

    def test_add_idempotent_updates_reason(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            run_cli(tmp, "add", "workspace:7", "first reason")
            run_cli(tmp, "add", "workspace:7", "second reason")
            data = json.loads((tmp / ".assistant/back-off.json").read_text())
            self.assertEqual(len(data["workspaces"]), 1)
            self.assertEqual(data["workspaces"][0]["reason"], "second reason")

    def test_remove_drops_entry(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            run_cli(tmp, "add", "workspace:7", "reason")
            run_cli(tmp, "add", "workspace:8", "another")
            r = run_cli(tmp, "remove", "workspace:7")
            self.assertEqual(r.returncode, 0)
            data = json.loads((tmp / ".assistant/back-off.json").read_text())
            self.assertEqual([w["ws_ref"] for w in data["workspaces"]], ["workspace:8"])

    def test_remove_nonexistent_returns_nonzero(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            r = run_cli(tmp, "remove", "workspace:never-added")
            self.assertEqual(r.returncode, 1)

    def test_add_rejects_bad_ref(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / ".assistant").mkdir()
            r = run_cli(tmp, "add", "112", "missing prefix")
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
