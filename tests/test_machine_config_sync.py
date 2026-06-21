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


class MapRun:
    """Fake subprocess.run that returns a per-script result keyed on whether the
    invoked script path is sync-push or sync-pull."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        key = "push" if "sync-push" in argv[1] else "pull"
        rc, out, err = self.results.get(key, (0, "", ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


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

    fake = MapRun()
    monkeypatch.setattr(mod.subprocess, "run", fake)
    return {"home": home, "repo": repo, "fake": fake, "monkeypatch": monkeypatch}


def test_skip_when_repo_missing(env, capsys):
    env["monkeypatch"].setattr(mod, "CONFIG_REPO", env["repo"].parent / "nope")
    assert mod.main() == 0
    assert env["fake"].calls == []
    assert "not found" in capsys.readouterr().err


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
        mod.subprocess, "run",
        MapRun({"push": (0, "sync-push: Nothing to push.\n", ""), "pull": (0, "sync-pull: done.\n", "")}),
    )
    assert mod.main() == 0
    data = json.loads(mod.LAST_RUN_PATH.read_text())
    assert data["pushed"] is False and data["pulled"] is False and data["rc"] == 0


def test_push_detected(env, capsys):
    env["monkeypatch"].setattr(
        mod.subprocess, "run",
        MapRun({"push": (0, "sync-push: reclaimed X\nsync-push: pushed.\n", ""), "pull": (0, "sync-pull: done.\n", "")}),
    )
    assert mod.main() == 0
    assert json.loads(mod.LAST_RUN_PATH.read_text())["pushed"] is True
    assert "pushed" in capsys.readouterr().out


def test_pull_detected(env, capsys):
    env["monkeypatch"].setattr(
        mod.subprocess, "run",
        MapRun({"push": (0, "sync-push: Nothing to push.\n", ""), "pull": (0, "Updating abc..def\nFast-forward\n", "")}),
    )
    assert mod.main() == 0
    assert json.loads(mod.LAST_RUN_PATH.read_text())["pulled"] is True
    assert "pulled config changes" in capsys.readouterr().out


def test_push_nonzero_returncode_propagates(env, capsys):
    env["monkeypatch"].setattr(
        mod.subprocess, "run",
        MapRun({"push": (7, "", "boom"), "pull": (0, "sync-pull: done.\n", "")}),
    )
    assert mod.main() == 7
    assert "boom" in capsys.readouterr().err
    assert json.loads(mod.LAST_RUN_PATH.read_text())["rc"] == 7
