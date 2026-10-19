"""bin/cmux_sessions.py runs a real `cmux` subprocess (tests/fake_cmux.py) and
returns typed session records, or [] whenever cmux can't answer."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cmux_sessions
import fake_cmux


@pytest.fixture
def cli(tmp_path, monkeypatch):
    directory = tmp_path / "cmux-bin"
    path = fake_cmux.install(directory)
    monkeypatch.setenv("CMUX_BIN", str(path))
    return directory


def test_lists_every_saved_record_with_typed_fields(cli, capsys):
    fake_cmux.write(cli, [
        fake_cmux.record(
            "f5ee1b35-11e2-4283-bc19-b9aec09efb58",
            workspace_id="7FA03E6B-BA29-4D3A-8655-FF0087CBFE7E",
            surface_id="13F23F97-EEDA-401D-852F-74DE856E6F2E",
            cwd="/Users/x/dev/assistant", launch_working_directory="/Users/x/dev/ffp",
            pid=47047, transcript_path="/Users/x/.claude/projects/p/f5ee1b35.jsonl",
            started_at="2026-10-18T15:48:38.207Z", updated_at_unix=1792418870.25,
            active_for_surface=True,
        ),
        fake_cmux.record("droid-1", agent="factory", active_for_surface=False),
        fake_cmux.record("codex-1", agent="codex"),
    ])
    sessions = cmux_sessions.list_sessions()
    assert fake_cmux.calls(cli) == [["sessions", "list", "--json", "--all"]]
    assert sessions[0] == cmux_sessions.Session(
        session_id="f5ee1b35-11e2-4283-bc19-b9aec09efb58",
        agent="claude",
        workspace_id="7FA03E6B-BA29-4D3A-8655-FF0087CBFE7E",
        surface_id="13F23F97-EEDA-401D-852F-74DE856E6F2E",
        cwd="/Users/x/dev/assistant",
        launch_cwd="/Users/x/dev/ffp",
        pid=47047,
        transcript_path="/Users/x/.claude/projects/p/f5ee1b35.jsonl",
        started_at=datetime(2026, 10, 18, 15, 48, 38, 207000, tzinfo=timezone.utc).timestamp(),
        updated_at=1792418870.25,
        active_for_surface=True,
    )
    assert [(s.session_id, s.provider, s.active_for_surface) for s in sessions] == [
        ("f5ee1b35-11e2-4283-bc19-b9aec09efb58", "claude", True),
        ("droid-1", "droid", False),
        ("codex-1", None, True),
    ]
    assert capsys.readouterr().err == ""


def test_skips_unusable_rows_and_blanks_unusable_fields(cli):
    fake_cmux.write(cli, [
        "not a record",
        {"agent": "claude"},
        fake_cmux.record(""),
        fake_cmux.record("bool-pid", pid=True, started_at="not a date",
                         updated_at_unix="1792418870", cwd="", workspace_id=None,
                         active_for_surface="yes"),
        fake_cmux.record("zero-pid", pid=0, started_at=None, agent=None),
        fake_cmux.record("naive-start", pid=-5, started_at="2026-10-18T15:48:38",
                         updated_at_unix=1792418870),
    ])
    sessions = {s.session_id: s for s in cmux_sessions.list_sessions()}
    assert list(sessions) == ["bool-pid", "zero-pid", "naive-start"]
    bad = sessions["bool-pid"]
    assert (bad.pid, bad.started_at, bad.updated_at, bad.cwd, bad.workspace_id,
            bad.active_for_surface) == (None, None, None, None, None, False)
    assert (sessions["zero-pid"].pid, sessions["zero-pid"].started_at,
            sessions["zero-pid"].agent, sessions["zero-pid"].provider) == (None, None, "", None)
    naive = sessions["naive-start"]
    assert naive.pid is None
    assert naive.started_at == datetime(2026, 10, 18, 15, 48, 38, tzinfo=timezone.utc).timestamp()
    assert naive.updated_at == 1792418870.0


@pytest.mark.parametrize("stdout,returncode,reason", [
    ('{"sessions": [{"session_id": "s"}]}', 1, "exit 1"),
    ("Error: sessions list: unknown agent", 0, "output is not JSON"),
    ('[{"session_id": "s"}]', 0, "output has no sessions list"),
    ('{"sessions": {"s": {}}}', 0, "output has no sessions list"),
])
def test_unusable_cli_answers_yield_no_sessions(cli, capsys, stdout, returncode, reason):
    fake_cmux.write(cli, stdout=stdout, returncode=returncode)
    assert cmux_sessions.list_sessions() == []
    assert len(fake_cmux.calls(cli)) == 1
    assert f"sessions list` unavailable ({reason}" in capsys.readouterr().err


def test_missing_cli_yields_no_sessions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CMUX_BIN", str(tmp_path / "no-cmux-here"))
    assert cmux_sessions.list_sessions() == []
    assert f"`{tmp_path / 'no-cmux-here'} sessions list` unavailable (" in capsys.readouterr().err


def test_hung_cli_times_out_to_no_sessions(tmp_path, monkeypatch, capsys):
    hung = tmp_path / "cmux"
    hung.write_text("#!/bin/sh\nexec sleep 30\n")
    hung.chmod(0o755)
    monkeypatch.setenv("CMUX_BIN", str(hung))
    monkeypatch.setattr(cmux_sessions, "TIMEOUT_SEC", 0.2)
    assert cmux_sessions.list_sessions() == []
    assert "timed out" in capsys.readouterr().err


def test_a_slow_cli_still_answers_within_the_timeout(tmp_path, monkeypatch):
    # cmux took 4.8 s to answer on a busy machine; a scan must not blank.
    directory = tmp_path / "slow"
    fake_cmux.install(directory, [fake_cmux.record("slow-but-there")])
    slow = directory / "slow-cmux"
    slow.write_text(f"#!/bin/sh\nsleep 6\nexec '{directory / 'cmux'}' \"$@\"\n")
    slow.chmod(0o755)
    monkeypatch.setenv("CMUX_BIN", str(slow))
    assert [s.session_id for s in cmux_sessions.list_sessions()] == ["slow-but-there"]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_cli_resolution_order(tmp_path, monkeypatch):
    bundled = _executable(tmp_path / "bundle/cmux")
    on_path = _executable(tmp_path / "path/cmux")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(bundled))
    monkeypatch.setenv("CMUX_BIN", str(tmp_path / "override"))
    assert cmux_sessions.cli_path() == str(tmp_path / "override")

    monkeypatch.delenv("CMUX_BIN")
    assert cmux_sessions.cli_path() == str(bundled)

    bundled.chmod(0o644)
    assert cmux_sessions.cli_path() == str(on_path)

    monkeypatch.delenv("CMUX_BUNDLED_CLI_PATH")
    assert cmux_sessions.cli_path() == str(on_path)

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cmux_sessions.cli_path() == cmux_sessions.APP_CLI


def test_bundled_cli_is_what_runs_without_an_override(tmp_path, monkeypatch):
    directory = tmp_path / "bundle"
    path = fake_cmux.install(directory, [fake_cmux.record("from-bundle")])
    monkeypatch.delenv("CMUX_BIN")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(path))
    monkeypatch.setenv("PATH", os.defpath)
    assert [s.session_id for s in cmux_sessions.list_sessions()] == ["from-bundle"]


def test_launch_arguments_become_a_tuple(cli):
    fake_cmux.write(cli, [
        fake_cmux.record("with-argv", launch_arguments=["/x/claude", "--model", "opus"]),
        fake_cmux.record("bad-argv", launch_arguments="not a list"),
    ])
    sessions = {s.session_id: s for s in cmux_sessions.list_sessions()}
    assert sessions["with-argv"].launch_argv == ("/x/claude", "--model", "opus")
    assert sessions["bad-argv"].launch_argv == ()
    assert {s.source for s in sessions.values()} == {cmux_sessions.CMUX}


def _registry(tmp_path, rows):
    path = tmp_path / "cmux-registry.json"
    path.write_text(rows if isinstance(rows, str) else json.dumps(rows))
    return path


def test_registry_fallback_keeps_only_sessions_cmux_lacks(tmp_path):
    path = _registry(tmp_path, {
        "WS-MISSED": {"session_id": "missed", "cwd": "/repo", "surface_id": "SF-1",
                      "tab_id": "WS-MISSED", "panel_id": "SF-1", "claude_pid": "",
                      "ts": 1792107522.13, "transcript_path": "/t/missed.jsonl"},
        "WS-KNOWN": {"session_id": "known", "surface_id": "SF-2", "claude_pid": "4242",
                     "ts": 10},
        "WS-PANEL": {"session_id": "panel-only", "panel_id": "SF-3", "claude_pid": 77,
                     "ts": "not a time"},
        "WS-NOSID": {"surface_id": "SF-4"},
        "WS-JUNK": "not a row",
    })
    known = [cmux_sessions.Session(
        session_id="known", agent="claude", workspace_id=None, surface_id=None, cwd=None,
        launch_cwd=None, pid=None, transcript_path=None, started_at=None, updated_at=None,
        active_for_surface=True)]
    rows = {s.session_id: s for s in cmux_sessions.registry_fallback(path, known)}
    assert list(rows) == ["missed", "panel-only"]
    assert rows["missed"] == cmux_sessions.Session(
        session_id="missed", agent="claude", workspace_id="WS-MISSED", surface_id="SF-1",
        cwd="/repo", launch_cwd="/repo", pid=None, transcript_path="/t/missed.jsonl",
        started_at=1792107522.13, updated_at=1792107522.13, active_for_surface=True,
        source=cmux_sessions.REGISTRY)
    panel = rows["panel-only"]
    assert (panel.surface_id, panel.pid, panel.started_at, panel.provider) == (
        "SF-3", 77, None, "claude")


@pytest.mark.parametrize("contents", [None, "{ not json", "[]"])
def test_unreadable_registry_yields_no_fallback(tmp_path, contents):
    path = tmp_path / "missing.json" if contents is None else _registry(tmp_path, contents)
    assert cmux_sessions.registry_fallback(path, []) == []


def test_agent_process_requires_a_live_claude_or_droid(tmp_path):
    impostor = tmp_path / "claude"
    impostor.symlink_to("/bin/sleep")
    process = subprocess.Popen([str(impostor), "30"])
    try:
        assert cmux_sessions.agent_process(process.pid) is True
    finally:
        process.kill()
        process.wait()
    assert cmux_sessions.agent_process(os.getpid()) is False  # a reused pid: python
    assert cmux_sessions.agent_process(process.pid) is False  # exited
    assert cmux_sessions.agent_process(None) is False


def test_agent_process_without_ps_is_false(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert cmux_sessions.agent_process(os.getpid()) is False
