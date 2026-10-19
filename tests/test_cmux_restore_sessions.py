"""bin/cmux-restore-sessions.py: resume commands per panel, and the recorded
session (cmux's record, else the registry row) for panels with no resume
binding. cmux is a fake executable (tests/fake_cmux.py) behind CMUX_BIN."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import fake_cmux

REPO = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "cmux_restore_sessions_test",
        REPO / "bin" / "cmux-restore-sessions.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def panel(kind="claude", arguments=None, binding=None):
    return {
        "id": "panel-1",
        "directory": "/tmp/repo",
        "terminal": {
            "surfaceId": "surface-1",
            "agent": {
                "kind": kind,
                "sessionId": "state-session",
                "workingDirectory": "/tmp/repo",
                "launchCommand": {"arguments": arguments or []},
            },
            "resumeBinding": binding or {},
        },
    }


def test_get_all_panels_accepts_claude_factory_and_droid():
    module = load_module()
    state = {
        "windows": [{
            "tabManager": {
                "workspaces": [{
                    "panels": [
                        panel("claude"),
                        panel("factory"),
                        panel("droid"),
                        panel("codex"),
                    ],
                }],
            },
        }],
    }
    found = list(module.get_all_panels(state))
    assert [p["terminal"]["agent"]["kind"] for _, p in found] == [
        "claude", "factory", "droid",
    ]


def test_claude_reuses_exact_previous_resume_binding():
    module = load_module()
    previous = "cd /tmp/repo && /custom/claude --resume state-session --model opus"
    cwd, command = module.build_resume_cmd(
        panel(
            "claude",
            arguments=["/custom/claude", "--model", "opus"],
            binding={
                "checkpointId": "state-session",
                "cwd": "/tmp/repo",
                "command": previous,
            },
        )
    )
    assert cwd == "/tmp/repo"
    assert command == previous


def test_factory_resume_preserves_args_and_enforces_glm_configuration(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    lessons = tmp_path / ".claude/CLAUDE.md"
    lessons.parent.mkdir(parents=True)
    lessons.write_text("rules")
    module = load_module()
    ledger = {
        "session_id": "ledger-session",
        "provider": "factory",
        "cwd": "/tmp/project with space",
        "launch_argv": [
            "/custom/droid",
            "--settings", "/old/settings.json",
            "--auto", "low",
            "--append-system-prompt-file", "/old/prompt.md",
            "--use-spec",
        ],
    }
    cwd, command = module.build_resume_cmd(
        panel("factory", arguments=["droid", "--auto", "medium"]),
        ledger,
    )
    assert cwd == "/tmp/project with space"
    assert command.startswith("cd '/tmp/project with space' && /custom/droid")
    assert "--resume ledger-session" in command
    assert "--use-spec" in command
    assert "/old/settings.json" not in command
    assert "/old/prompt.md" not in command
    assert "droid-glm-settings.json" in command
    assert "--auto high" in command
    assert "--append-system-prompt-file" in command


def test_factory_rebuilds_unconfigured_old_binding():
    module = load_module()
    old = "cd /tmp/repo && droid --resume state-session"
    _, command = module.build_resume_cmd(
        panel(
            "droid",
            arguments=["droid"],
            binding={
                "checkpointId": "state-session",
                "cwd": "/tmp/repo",
                "command": old,
            },
        )
    )
    assert command != old
    assert "droid-glm-settings.json" in command
    assert "--resume state-session" in command


def test_recorded_entry_without_provider_fails_closed():
    module = load_module()
    old_panel = panel("", arguments=[])
    cwd, command = module.build_resume_cmd(
        old_panel, {"session_id": "old-session", "cwd": "/tmp/repo"})
    assert cwd is None
    assert command is None


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """A fake cmux plus an empty registry; returns (module, cmux dir)."""
    module = load_module()
    cli_dir = tmp_path / "cmux-bin"
    monkeypatch.setenv("CMUX_BIN", str(fake_cmux.install(cli_dir)))
    module.REGISTRY = tmp_path / "cmux-registry.json"
    return module, cli_dir


def test_load_recorded_takes_the_newest_session_per_panel(recorded):
    module, cli_dir = recorded
    fake_cmux.write(cli_dir, [
        fake_cmux.record("droid-sid", agent="factory", surface_id="PANEL-D", cwd="/d/sub",
                         launch_working_directory="/d", pid=55,
                         launch_arguments=["droid", "--auto", "low"], updated_at_unix=30,
                         transcript_path="/f/droid-sid.jsonl"),
        fake_cmux.record("older-claude", surface_id="PANEL-D", updated_at_unix=5),
        fake_cmux.record("stale-cmux", surface_id="PANEL-C", updated_at_unix=10),
        fake_cmux.record("cleared", surface_id="PANEL-X", active_for_surface=False),
        fake_cmux.record("codex", agent="codex", surface_id="PANEL-Y"),
    ])
    module.REGISTRY.write_text(json.dumps({
        "WS-C": {"session_id": "newer-registry", "surface_id": "PANEL-C", "ts": 20,
                 "cwd": "/c", "claude_pid": "", "transcript_path": "/t/c.jsonl"},
        "WS-R": {"session_id": "registry-only", "surface_id": "PANEL-R", "ts": 5, "cwd": "/r"},
    }))
    entries = module.load_recorded()
    assert sorted(entries) == ["PANEL-C", "PANEL-D", "PANEL-R"]
    assert entries["PANEL-D"] == {
        "session_id": "droid-sid", "cwd": "/d", "provider": "factory",
        "launch_argv": ["droid", "--auto", "low"], "transcript_path": "/f/droid-sid.jsonl",
        "pid": 55,
    }
    assert entries["PANEL-C"]["session_id"] == "newer-registry"
    assert entries["PANEL-R"]["provider"] == "claude"


def test_dry_run_restores_a_panel_with_no_resume_binding(recorded, tmp_path, monkeypatch,
                                                        capsys):
    # ee4ee641's panel on 2026-10-19: no agent state, no resume binding, and no
    # cmux record. Only the registry knows its session.
    module, _ = recorded
    module.CMUX_STATE = tmp_path / "state.json"
    module.CMUX_STATE.write_text(json.dumps({"windows": [{"tabManager": {"workspaces": [{
        "customTitle": "Fix floodgate [27]",
        "panels": [{"id": "5C0A8B6C", "terminal": {}}],
    }]}}]}))
    module.REGISTRY.write_text(json.dumps({"WS-27": {
        "session_id": "ee4ee641-8595", "surface_id": "5C0A8B6C", "ts": 1,
        "cwd": "/Users/x/dev/firefly-platform", "claude_pid": "",
    }}))
    monkeypatch.setattr(module, "pgrep_session", lambda sid: False)
    monkeypatch.setattr(sys, "argv", ["cmux-restore-sessions.py", "--dry-run", "--verbose"])
    module.main()
    out = capsys.readouterr().out
    assert "Recorded sessions: 1" in out
    assert ("[DRY-RUN] Would send: cd /Users/x/dev/firefly-platform && "
            "claude --resume ee4ee641-8595") in out
    assert "Summary: 1 restored, 0 skipped, 1 dead" in out


def test_ledger_transcript_can_identify_factory():
    module = load_module()
    ledger = {
        "session_id": "sid",
        "transcript_path": "/Users/test/.factory/sessions/repo/sid.jsonl",
    }
    _, command = module.build_resume_cmd(panel("", arguments=[]), ledger)
    assert "droid --resume sid" in command
    assert "droid-glm-settings.json" in command
