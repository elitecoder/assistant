"""Coverage top-up for bin/self_update.py — fills the branches the existing
tests/test_self_update.py leaves open:

  - _stash_dirty when `git stash push` returns rc != 0 (line 88).
  - maybe_update stash-failed path (278-281).
  - maybe_update pull-failed path (291-294).
  - the changed-but-to_sha-set early return when the pull is a no-op (296-300).
  - the __main__ CLI block (348-366) via a real subprocess against a tmp git
    repo with NO remote (no network, fast, read-only).

The git-failure branches are driven by monkeypatching the module-level `_git`
helper to return canned tuples — no real network, no real fetch where the
failure can't be produced cleanly with real git.
"""
from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

from pathlib import Path as _P

REPO = _P(__file__).resolve().parent.parent
SELF_UPDATE_PATH = REPO / "bin/self_update.py"


def load_module():
    spec = importlib.util.spec_from_file_location("self_update_topup_mod", str(SELF_UPDATE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


su = load_module()


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


def _make_solo_repo(tmp: Path) -> Path:
    """A git repo with one commit and NO remote — maybe_update returns a
    no-remote skip, exercising the CLI block end-to-end with zero network."""
    solo = tmp / "solo"
    solo.mkdir()
    _git(solo, "init", "-b", "main")
    _git(solo, "config", "user.email", "t@t")
    _git(solo, "config", "user.name", "t")
    (solo / "f").write_text("x")
    _git(solo, "add", "-A")
    _git(solo, "commit", "-m", "init")
    return solo


# ─── _stash_dirty rc != 0 ─────────────────────────────────────────────────────

def test_stash_dirty_failure_returns_false():
    with mock.patch.object(su, "_git", return_value=(1, "", "stash refused: conflict")):
        ok, detail = su._stash_dirty(Path("/tmp/repo"), "label")
    assert ok is False
    assert "stash refused" in detail


def test_stash_dirty_success_returns_true():
    with mock.patch.object(su, "_git", return_value=(0, "Saved working directory", "")):
        ok, detail = su._stash_dirty(Path("/tmp/repo"), "label")
    assert ok is True
    assert "Saved working directory" in detail


# ─── maybe_update: stash-failed path (278-281) ────────────────────────────────

def test_maybe_update_stash_failed(tmp_path):
    """Dirty past the window, an update waiting, but git stash refuses →
    skipped_reason 'stash-failed', work untouched, no pull attempted."""
    marker = tmp_path / "m.json"

    # resolve_remote_branch → (origin, main); repo_status → dirty + behind.
    with mock.patch.object(su, "resolve_remote_branch", return_value=("origin", "main")):
        with mock.patch.object(su, "repo_status", return_value={
                "head": "oldsha", "remote_sha": "newsha",
                "dirty": True, "behind": 1, "ahead": 0}):
            with mock.patch.object(su, "_stash_dirty",
                                   return_value=(False, "fatal: stash conflict")):
                # First pass stamps dirty_since at t=1000.
                su.maybe_update(tmp_path, interval_sec=0, marker_path=marker,
                                dirty_stash_after_sec=86400, now=1000.0)
                # 25h later → past window → tries to stash → fails.
                r = su.maybe_update(tmp_path, interval_sec=0, marker_path=marker,
                                    dirty_stash_after_sec=86400,
                                    now=1000.0 + 25 * 3600)
    assert r["skipped_reason"] == "stash-failed"
    assert r["stashed"] is False
    assert "stash conflict" in r["error"]
    assert r["changed"] is False


# ─── maybe_update: pull-failed path (291-294) ─────────────────────────────────

def test_maybe_update_pull_failed(tmp_path):
    """Clean tree, behind, but `git pull --ff-only` fails (e.g. diverged) →
    skipped_reason 'pull-failed' with the git error captured."""
    marker = tmp_path / "m.json"

    def fake_git(repo, *args, **kw):
        if args[:1] == ("pull",):
            return (1, "", "fatal: Not possible to fast-forward, aborting.")
        # rev-parse HEAD after a failed pull would not be reached.
        return (0, "", "")

    with mock.patch.object(su, "resolve_remote_branch", return_value=("origin", "main")):
        with mock.patch.object(su, "repo_status", return_value={
                "head": "oldsha", "remote_sha": "newsha",
                "dirty": False, "behind": 2, "ahead": 0}):
            with mock.patch.object(su, "_git", side_effect=fake_git):
                r = su.maybe_update(tmp_path, interval_sec=0, marker_path=marker,
                                    now=1000.0)
    assert r["skipped_reason"] == "pull-failed"
    assert "fast-forward" in r["error"]
    assert r["changed"] is False


# ─── maybe_update: pull succeeded but HEAD unchanged (296-300) ────────────────

def test_maybe_update_pull_noop_to_sha_set(tmp_path):
    """A successful ff-only pull that leaves HEAD unchanged (e.g. remote ref was
    stale) → changed False, to_sha stamped, returns before classify/install."""
    marker = tmp_path / "m.json"

    def fake_git(repo, *args, **kw):
        if args[:1] == ("pull",):
            return (0, "Already up to date.", "")
        if args[:2] == ("rev-parse", "HEAD"):
            return (0, "oldsha", "")  # same as status head → no change
        return (0, "", "")

    with mock.patch.object(su, "resolve_remote_branch", return_value=("origin", "main")):
        with mock.patch.object(su, "repo_status", return_value={
                "head": "oldsha", "remote_sha": "newsha",
                "dirty": False, "behind": 1, "ahead": 0}):
            with mock.patch.object(su, "_git", side_effect=fake_git):
                r = su.maybe_update(tmp_path, interval_sec=0, marker_path=marker,
                                    now=1000.0)
    assert r["changed"] is False
    assert r["to_sha"] == "oldsha"[:12]
    # Did NOT proceed to classify/install — no files_changed / needs_install.
    assert "files_changed" not in r
    assert "needs_install" not in r


# ─── __main__ CLI block (348-366) ─────────────────────────────────────────────

def test_cli_main_block_runs_against_solo_repo(tmp_path, capsys, monkeypatch):
    """Execute the `if __name__ == "__main__"` CLI block in-process via runpy
    (so coverage records it) with `--repo <solo> --force`. The repo has no
    remote, so maybe_update returns a no-remote skip — exercises the argparse +
    maybe_update + json.dumps + sys.exit(0) lines. No network, no real pull."""
    solo = _make_solo_repo(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["self_update.py", "--repo", str(solo), "--force"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SELF_UPDATE_PATH), run_name="__main__")
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped_reason"] == "no-remote"
    assert out["attempted"] is True


def test_cli_main_block_subprocess_smoke():
    """Belt-and-braces: the CLI also works as a real subprocess entrypoint
    (no coverage credit for the child process, but proves the shebang path)."""
    with TemporaryDirectory() as t:
        tmp = Path(t)
        solo = _make_solo_repo(tmp)
        proc = subprocess.run(
            [sys.executable, str(SELF_UPDATE_PATH), "--repo", str(solo), "--force"],
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["skipped_reason"] == "no-remote"
