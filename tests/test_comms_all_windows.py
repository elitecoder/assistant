"""The comms daemon checks its warm workspace in every cmux window.

A bare `cmux list-workspaces` answers for the caller's window, and the daemon
has none, so cmux answered for the focused window. On 2026-11-21 the live warm
session sat in another window: the daemon called it gone, spawned a second one,
and skipped closing the first because it couldn't see it. These tests run the
real cmux calls against a fake `cmux` whose bare listing shows only the focused
("key") window, as the live one does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import comms_lib as cl
import comms_session as cs
import pytest

import fake_cmux

BIN = Path(__file__).resolve().parent.parent / "bin"
REF = "workspace:1000020126"
USER_WORK = "  workspace:1000020144  Fix duplicate comms workspaces\n"


def _paths(tmp_path, monkeypatch, windows, **kwargs):
    cli = fake_cmux.install_windowed(tmp_path / "cmux", windows, key="KEY", **kwargs)
    monkeypatch.setenv("CMUX_BIN", str(cli))
    return cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path),
                              "CMUX_BIN": str(cli)})


def _warm_in_other_window(paths_title: str) -> dict[str, str]:
    return {"KEY": USER_WORK, "OTHER": f"* {REF}  {paths_title} [3]  [selected]\n"}


def _title(tmp_path):
    return cs.warm_workspace_title(cl.Paths.from_env(
        {"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)}))


def test_probe_finds_a_warm_session_in_another_window(tmp_path, monkeypatch):
    paths = _paths(tmp_path, monkeypatch, _warm_in_other_window(_title(tmp_path)))
    assert cs.probe_workspace(paths, REF) == cs.ALIVE


def test_probe_calls_a_ref_gone_only_when_no_window_lists_it(tmp_path, monkeypatch):
    paths = _paths(tmp_path, monkeypatch, _warm_in_other_window(_title(tmp_path)))
    assert cs.probe_workspace(paths, "workspace:1000020999") == cs.GONE


def test_probe_is_unknown_when_any_window_fails_to_list(tmp_path, monkeypatch):
    """A window that can't be listed might hold the session, so a partial
    listing must not call it gone."""
    paths = _paths(tmp_path, monkeypatch, _warm_in_other_window(_title(tmp_path)),
                   failing=["OTHER"])
    assert cs.probe_workspace(paths, REF) == cs.UNKNOWN


def test_close_own_workspace_closes_a_warm_session_in_another_window(tmp_path, monkeypatch):
    paths = _paths(tmp_path, monkeypatch, _warm_in_other_window(_title(tmp_path)))
    logs: list[str] = []
    cs.close_own_workspace(paths, REF, log=logs.append)
    assert logs == [f"closed prior warm workspace {REF}"]
    assert ["close-workspace", "--workspace", REF] in fake_cmux.calls(tmp_path / "cmux")


def test_close_own_workspace_keeps_its_title_guard_across_windows(tmp_path, monkeypatch):
    paths = _paths(tmp_path, monkeypatch, {"KEY": "", "OTHER": USER_WORK})
    logs: list[str] = []
    cs.close_own_workspace(paths, "workspace:1000020144", log=logs.append)
    assert logs == ["skip close workspace:1000020144: not a "
                    f"'{cs.SESSION_TITLE}' workspace (ref reissued?)"]
    assert not any(c[0] == "close-workspace" for c in fake_cmux.calls(tmp_path / "cmux"))


def test_orphan_scan_finds_a_warm_workspace_in_another_window(tmp_path, monkeypatch):
    paths = _paths(tmp_path, monkeypatch, _warm_in_other_window(_title(tmp_path)))
    assert cs.list_warm_workspaces(paths) == [REF]


def _load_listen():
    spec = importlib.util.spec_from_file_location(
        "comms_listen_all_windows", str(BIN / "comms-listen.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comms_listen_all_windows"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def listen(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_listen()
    yield mod
    sys.modules.pop("comms_listen_all_windows", None)


def test_daemon_keeps_a_live_warm_session_in_another_window(tmp_path, monkeypatch, listen):
    """The 2026-11-21 leak: the watchdog's check must see the session and leave
    it alone, not spawn a second warm workspace."""
    paths = _paths(tmp_path, monkeypatch, _warm_in_other_window(_title(tmp_path)))
    sess = {"ws_ref": REF, "surface_ref": "surface:1000020222", "cwd": "/c", "agent": "claude"}
    spawned: list = []
    monkeypatch.setattr(listen.comms_session, "read_session", lambda p: sess)
    monkeypatch.setattr(listen.comms_session, "clear_session_registry", lambda p: None)
    monkeypatch.setattr(listen.comms_session, "spawn_session",
                        lambda p, prompt, log=None: spawned.append(1) or {"ws_ref": "workspace:2"})

    assert listen._warm_session(paths) == (sess, listen.SESSION_ALIVE)
    assert spawned == []
    assert not any(c[0] == "close-workspace" for c in fake_cmux.calls(tmp_path / "cmux"))


def test_probe_is_unknown_when_cmux_lists_windows_unreadably():
    paths = cl.Paths.from_env({"HOME": "/h", "COMMS_HOME": "/h"})

    def run(argv, timeout=30):
        if argv[1] == "tree":
            return 1, "", "Error: Failed to connect to socket (errno 61)"
        return 0, "cmux: 'list-windows' is now an alias", ""

    assert cs.probe_workspace(paths, REF, run=run) == cs.UNKNOWN
