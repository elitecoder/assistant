"""Tests for bin/machine-config-sync.py.

Loaded by file path (hyphenated CLI). Every test runs against tmp paths — the
module-level constants are monkeypatched under tmp_path and subprocess.run is
faked, so no real bash / sync scripts and no real ~/.assistant are touched.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(_BIN / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load("machine_config_sync", "machine-config-sync.py")


class FakeRun:
    """Fake mod._run_script keyed on the label (sync-push / sync-pull). Returns
    the (rc, stdout, stderr, timed_out) tuple the real helper does; records the
    labels it was called with so tests can assert pull is SKIPPED after a failed
    push."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls: list[str] = []

    def __call__(self, path, label, env, timeout_s):
        self.calls.append(label)
        key = "push" if label == "sync-push" else "pull"
        rc, out, err = self.results.get(key, (0, "", ""))
        return (rc, out, err, rc == 124)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    repo = tmp_path / "machine-config"
    (repo / "scripts").mkdir(parents=True)
    push = repo / "scripts" / "sync-push.sh"
    pull = repo / "scripts" / "sync-pull.sh"
    push.write_text("#!/bin/bash\n")
    pull.write_text("#!/bin/bash\n")

    monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(mod, "CONFIG_REPO", repo)
    monkeypatch.setattr(mod, "SYNC_PUSH", push)
    monkeypatch.setattr(mod, "SYNC_PULL", pull)
    monkeypatch.setattr(mod, "LAST_RUN_PATH", home / ".assistant" / "machine-config-sync-last.json")
    # This box is OPTED IN (the marker exists) — the default for the sync tests.
    marker = home / ".assistant" / "machine-config-configured"
    marker.write_text("2026-01-01T00:00:00Z\n")
    monkeypatch.setattr(mod, "CONFIGURED_MARKER", marker)

    fake = FakeRun()
    monkeypatch.setattr(mod, "_run_script", fake)
    return {"home": home, "repo": repo, "marker": marker, "fake": fake,
            "monkeypatch": monkeypatch}


def test_not_opted_in_is_a_noop(env):
    # No opt-in marker → the wrapper refuses to sync, EVEN with the repo present
    # and scripts in place. This is the real gate: launchd auto-loads the plist
    # at login regardless of install.sh, so the runtime must enforce opt-in.
    env["marker"].unlink()
    assert mod.main() == 0
    assert env["fake"].calls == []           # nothing spawned
    assert not mod.LAST_RUN_PATH.exists()     # and no marker/throttle churn


def test_run_script_kills_process_group_on_timeout(tmp_path):
    # Exercise the REAL _run_script (not the FakeRun): a hanging child must be
    # process-group-killed and rc=124/timed_out returned PROMPTLY — the killpg
    # path both reviews flagged as untested.
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/bash\nsleep 30\n")
    t0 = time.time()
    rc, out, err, timed = mod._run_script(slow, "slow", {"PATH": "/bin:/usr/bin"}, 1)
    assert timed is True and rc == 124
    assert time.time() - t0 < 15  # returned promptly; the child was killed, not waited on


def test_skip_when_repo_missing(env, capsys):
    env["monkeypatch"].setattr(mod, "CONFIG_REPO", env["repo"].parent / "nope")
    assert mod.main() == 0
    assert env["fake"].calls == []
    assert "not found" in capsys.readouterr().err


def test_missing_scripts_fail_loud_no_marker(env, capsys):
    # Repo present but scripts gone (a config-repo restructure) → FAIL LOUD
    # (rc=1, stderr) and DON'T write the throttle marker, so it stays visible and
    # retries — not a silent rc=0-forever no-op (review).
    env["monkeypatch"].setattr(mod, "SYNC_PUSH", env["repo"] / "scripts" / "gone.sh")
    assert mod.main() == 1
    assert env["fake"].calls == []
    assert "not syncable" in capsys.readouterr().err
    assert not mod.LAST_RUN_PATH.exists()


def test_push_timeout_survived_and_pull_skipped(env):
    # A timed-out push (rc 124) is surfaced, NOT a hang — and pull is SKIPPED so
    # it can't run/re-project over a repo the killed push may have left mid-
    # mutation / index.lock'd (review: the timeout must also skip the pull).
    f = FakeRun({"push": (124, "", "")})
    env["monkeypatch"].setattr(mod, "_run_script", f)
    assert mod.main() == 124
    assert f.calls == ["sync-push"]  # pull skipped


def test_loop_guard_short_circuits(env, monkeypatch):
    # An inherited MACHINE_CONFIG_SYNC_IN_PROGRESS must abort before any work.
    monkeypatch.setenv("MACHINE_CONFIG_SYNC_IN_PROGRESS", "1")
    assert mod.main() == 0
    assert env["fake"].calls == []
    assert not mod.LAST_RUN_PATH.exists()


def test_throttle_skips(env):
    mod.LAST_RUN_PATH.write_text(json.dumps({"ts": time.time()}))
    assert mod.main() == 0
    assert env["fake"].calls == []


def test_malformed_last_run_proceeds(env):
    mod.LAST_RUN_PATH.write_text("{not json")
    assert mod.main() == 0
    assert len(env["fake"].calls) == 2  # push + pull


def test_clean_run_records_no_movement(env):
    env["monkeypatch"].setattr(
        mod, "_run_script",
        FakeRun({"push": (0, "sync-push: Nothing to push.\n", ""), "pull": (0, "sync-pull: done.\n", "")}),
    )
    assert mod.main() == 0
    data = json.loads(mod.LAST_RUN_PATH.read_text())
    assert data["pushed"] is False and data["pulled"] is False and data["rc"] == 0


def test_push_detected(env, capsys):
    env["monkeypatch"].setattr(
        mod, "_run_script",
        FakeRun({"push": (0, "sync-push: reclaimed X\nsync-push: pushed.\n", ""), "pull": (0, "sync-pull: done.\n", "")}),
    )
    assert mod.main() == 0
    assert json.loads(mod.LAST_RUN_PATH.read_text())["pushed"] is True
    assert "pushed" in capsys.readouterr().out


def test_pull_detected(env, capsys):
    env["monkeypatch"].setattr(
        mod, "_run_script",
        FakeRun({"push": (0, "sync-push: Nothing to push.\n", ""), "pull": (0, "Updating abc..def\nFast-forward\n", "")}),
    )
    assert mod.main() == 0
    assert json.loads(mod.LAST_RUN_PATH.read_text())["pulled"] is True
    assert "pulled config changes" in capsys.readouterr().out


def test_push_failure_skips_pull_and_propagates(env, capsys):
    f = FakeRun({"push": (7, "", "boom"), "pull": (0, "sync-pull: done.\n", "")})
    env["monkeypatch"].setattr(mod, "_run_script", f)
    assert mod.main() == 7
    assert f.calls == ["sync-push"]  # pull NOT run after a failed push (review)
    err = capsys.readouterr().err
    assert "boom" in err and "skipping pull" in err
    assert json.loads(mod.LAST_RUN_PATH.read_text())["rc"] == 7


def test_bad_timeout_env_does_not_crash(env, monkeypatch):
    monkeypatch.setenv("MACHINE_CONFIG_SYNC_TIMEOUT", "5m")
    assert mod.main() == 0  # bad value → 300s fallback, not a ValueError traceback
