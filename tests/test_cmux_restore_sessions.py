from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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


def test_old_ledger_row_without_provider_fails_closed(tmp_path):
    module = load_module()
    module.LEDGER = tmp_path / "ledger.jsonl"
    module.LEDGER.write_text(json.dumps({
        "event": "start",
        "ts": 1,
        "session_id": "old-session",
        "panel_id": "panel-1",
        "cwd": "/tmp/repo",
    }) + "\n")
    ledger = module.load_ledger()
    old_panel = panel("", arguments=[])
    cwd, command = module.build_resume_cmd(old_panel, ledger["panel-1"])
    assert cwd is None
    assert command is None


def test_ledger_transcript_can_identify_factory():
    module = load_module()
    ledger = {
        "session_id": "sid",
        "transcript_path": "/Users/test/.factory/sessions/repo/sid.jsonl",
    }
    _, command = module.build_resume_cmd(panel("", arguments=[]), ledger)
    assert "droid --resume sid" in command
    assert "droid-glm-settings.json" in command
